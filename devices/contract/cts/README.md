# CTS — Compatibility Test Suite

`devices/contract/cts/` is the automated enforcer of [`devices/contract/COMPATIBILITY.md`](../COMPATIBILITY.md).
The spec *defines* what an Autonomous-compatible device must do; this suite *verifies* it.
Passing the CTS is what lets a device use the Autonomous-compatible mark and run the skill
catalog — the mechanism that keeps a hundreds-of-device ecosystem coherent (Android's CTS).

The suite has two halves, and the split matters: the static half proves a device is *described*
correctly, the runtime half proves the *running* device matches that description. A declaration
passes the static half whether or not any hardware ever booted it.

## Static half — `test_compatibility.py`

```bash
make cts          # or: python3 -m unittest discover -s devices/contract/cts -v
```

Validates every device under `devices/` against the MUST rules it can read off `ROBOT.md`:
schema, the `system` capability, a primary sense/output, a known capability vocabulary, and a
`SAFETY.md` for any safety-class capability. No hardware, no dependencies — this is what CI
runs on every push.

## Runtime half — `test_runtime.py`

```bash
make cts-runtime TARGET=lamp-ac82.local
```

Points at a provisioned device and compares **what it reports mounted** against **what its
`ROBOT.md` declares**. Every test skips unless `CTS_HAL` is set, so CI (no hardware) stays
green and the same `unittest discover` command runs both halves.

| Environment | Meaning |
|-------------|---------|
| `CTS_HAL` | HAL base URL (`http://<device>:5001`). Unset → the whole runtime half skips. |
| `CTS_OS` | os-server base URL (`http://<device>:5000`). Unset → the envelope rule skips. |
| `CTS_DEVICES_DIR` | where `devices/<id>/ROBOT.md` lives (default: this checkout). Set it to `/opt/devices` to run on the device itself. |
| `CTS_TIMEOUT` | per-request timeout in seconds (default 5). |
| `CTS_STOP_BUDGET_MS` | assert the deterministic stop answers within N ms. Unset → measured and printed, not asserted. |
| `CTS_ALLOW_MOTION=1` | also exercise stops that MOVE hardware. **Off by default: `/servo/release` cuts torque, which drops a raised arm.** |

### Reaching the device

Both services bind loopback on a provisioned device — os-server at `127.0.0.1:5000`
(`system/server/server.go`) and HAL at `127.0.0.1:5001` (`hal/config.py`, production mode). So
`TARGET=<hostname>` from a laptop will not connect. Two ways in, neither needing an API token
(HAL has no auth, and `/api/health/live` is outside `adminAuthMiddleware`):

```bash
# A — tunnel from your machine, suite runs against this checkout
ssh -N -L 5001:127.0.0.1:5001 -L 5000:127.0.0.1:5000 <user>@<device-ip>   # terminal 1
make cts-runtime TARGET=127.0.0.1                                        # terminal 2

# B — run it on the device, no forwarding at all (hal lives at /opt/hal,
#     the profile at /opt/devices/<type>)
scp devices/contract/cts/test_runtime.py <user>@<device-ip>:/tmp/
ssh <user>@<device-ip> 'cd /tmp && PYTHONPATH=/opt CTS_DEVICES_DIR=/opt/devices \
  CTS_HAL=http://127.0.0.1:5001 CTS_OS=http://127.0.0.1:5000 \
  python3 -m unittest test_runtime -v'
```

What it enforces:

| Rule | Check |
|------|-------|
| MUST 1 | `GET /device` serves id/name/type/schema/board; the booted board is one the `ROBOT.md` lists; the served id matches the declaration |
| MUST 2 | the `system` capability answers (`GET /health`) |
| MUST 3 | a declared primary sense (`audio`/`vision`) actually has a mounted route |
| MUST 5 | every `required` route is mounted — a device serving requests with a required route missing is the silent half-boot the rule forbids — and mounted routes answer a read-only probe |
| MUST 7 | the success envelope is exactly `{"status":1,"data":…,"message":null}` |
| MUST NOT 15 / MUST 6 | a device declaring `motion` answers a deterministic stop; its latency is reported |
| MUST NOT 16 | nothing is mounted that `ROBOT.md` does not declare (modulo HAL's always-on routes) |

When to run it: after bringing up a **new device** (before claiming it works — this is what
would have caught a `ROBOT.md` that describes hardware nobody booted), when a third party
wants the compatible mark on their own hardware, and as a regression pass over the lab devices
after a HAL change. It needs a device, so it is not a CI gate.

## Not covered yet

- **MUST 6's "within budget" has no number.** `COMPATIBILITY.md` says the stop must be
  "immediate"; the spec names no millisecond bound, so this suite measures and prints the
  latency and only asserts it when you supply `CTS_STOP_BUDGET_MS`. Closing this means putting
  a number in the spec first — inventing one here would be the test writing the contract.
- **MUST 8 (local setup, no cloud round-trip)** — needs driving the provisioning flow itself,
  not a probe against an already-provisioned device.
- **MUST NOT 14 (no safety-critical stop routed through the LLM)** — an architectural property;
  observable only by fault injection with the runtime stopped.
- **Route probes are a known list.** `bluetooth`, `speaker` and `system` have no read-only
  probe in `ROUTE_PROBES`, so for those "mounted" is taken at the device's word.
- **`stop_always: true`** in `SAFETY.md` is parsed into `MotionBounds` but no HAL route consults
  it — the guarantee holds today because nothing gates stop, not because anything enforces it.
