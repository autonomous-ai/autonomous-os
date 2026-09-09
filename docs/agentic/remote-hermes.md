# Remote Hermes — use Intern 2 as a voice frontend for Hermes on your Mac

An Intern 2 device can act as a voice/chat frontend for a Hermes server running on your Mac, instead of running its own local agent. The device captures voice, the Mac runs the brain, and the response is spoken back on the device — all over your local Wi-Fi.

This uses the device's existing Hermes runtime, retargeted at the Mac's LAN address. There is no separate "remote agent" software to install; a small setup script on the Mac enables Hermes's built-in HTTP API and exposes it on the LAN.

---

## What you need

- A **Mac** with the Hermes agent CLI installed (the [grid](https://grid.dev) install works out of the box; a plain `pip`/`uv` install also works if `hermes` is on your `PATH`).
- An **Intern 2 device** running an OS build that includes the "Remote (external)" runtime option in **Settings → Runtime**. Ship dates: OS versions after 2026-09-09.
- The Mac and the Intern 2 on the **same Wi-Fi network** (or a wired LAN — anything where they can reach each other by IP).
- No account setup, no cloud credentials — the connection is direct LAN traffic between the two machines.

---

## Setup — the two-step version

### 1. On the Mac — one command

```bash
curl -fsSL https://cdn.autonomous.ai/os/tools/setup-remote-hermes.sh | bash
```

The script:

1. Finds your Hermes install.
2. Adds `aiohttp` to Hermes's Python environment if missing (Hermes's API server refuses to bind without it).
3. Writes `API_SERVER_ENABLED=true` + a randomly-generated API key into `~/.hermes/.env`. If you run it more than once, the key is kept — re-running never rotates an in-use token.
4. Starts (or restarts) the Hermes gateway on `0.0.0.0:8642` so the device can reach it over the LAN.
5. Prints the two fields you need to paste into the device:

```
════════════════════════════════════════════════════════════
  Paste these into your Intern 2 web setup page
  (Settings → Runtime → Remote (external))
════════════════════════════════════════════════════════════

  Hermes URL : http://192.168.1.42:8642
  API Key    : intern2-hermes-a1b2c3d4e5f6a7b8
```

(Your real IP + key will be different. Copy them exactly.)

### 2. On the Intern 2 — paste into the web UI

1. Open the device's web setup page in a browser (`http://<device-ip>` on the same Wi-Fi, or the setup portal you used at first boot).
2. Go to **Settings → Runtime**.
3. In the "Backend" dropdown, choose **Remote (external)**. Two fields appear:
   - **Hermes URL** — paste `http://…:8642` from the Mac script's output.
   - **API Key** — paste the token from the same output.
4. Click **Switch to Remote (external)** and confirm.
5. The device restarts its os-server (~5 seconds). Wait for the runtime status to show **Active**.

That's it. Open the device's Chat page and send a message. The brain now runs on the Mac.

---

## How to check it's actually working

Three ways to confirm the device is really talking to the Mac's Hermes, in order of easiest → most detailed:

### (a) See connections coming in

On the Mac:

```bash
lsof -iTCP:8642 -sTCP:ESTABLISHED -n
```

You should see one or more `ESTABLISHED` connections whose remote end is the device's IP. `/health` polls keep the connection warm even when nothing is being chatted.

### (b) Tail the Hermes access log during a chat

The Hermes gateway logs every incoming HTTP request when it's running in verbose mode. The `setup-remote-hermes.sh` script starts the gateway with default verbosity; every chat produces one `POST /v1/responses` line:

```bash
tail -f /tmp/hermes-gateway.log | grep -E 'aiohttp\.access.*(POST|GET /health)'
```

Send a chat message in the device's web UI. Each message should print one line like:

```
INFO aiohttp.access: 192.168.1.10 [09/Sep/2026:14:37:12 +0700] "POST /v1/responses HTTP/1.1" 200 12273
```

If you only see `GET /health` lines and no `POST /v1/responses`, the chat isn't reaching the Mac — check the "Troubleshoot" section below.

### (c) Curl the endpoint yourself

From any machine on the LAN (including the Mac):

```bash
curl -H 'Authorization: Bearer YOUR_TOKEN' http://YOUR_MAC_IP:8642/health
```

You should get `{"status": "ok", "platform": "hermes-agent", "version": "…"}`. If curl times out or returns a network error, the Mac isn't reachable on that IP/port — a firewall or a mistyped IP is the usual cause.

---

## Troubleshoot

### `agent gateway not connected` in the device chat
The device restarted its runtime but the gateway hasn't come up yet, or the URL/token is wrong.

1. Reload the Runtime settings page — the status should say **Active** (not **Starting…** or an error) within 10 seconds after the switch.
2. Re-run `curl` from the device to verify reachability:
   ```bash
   ssh …@<device-ip>   # or use the device's built-in web CLI
   curl -m 5 -H 'Authorization: Bearer YOUR_TOKEN' http://YOUR_MAC_IP:8642/health
   ```
   Expected: `{"status": "ok", …}`. If it times out, the URL, port, firewall, or LAN routing is wrong.

### The setup script says `Port 8642 already in use`
An earlier Hermes gateway is still running. The script tries to stop it and wait for the socket to release, but macOS holds sockets in `TIME_WAIT` briefly. Re-run the script — the second run usually succeeds, or wait 30 seconds and try again.

### The setup script can't find Hermes or its Python interpreter
The script walks known install locations (grid, pipx, brew, `~/.hermes/hermes-agent/venv`, …); a Hermes install outside those needs an explicit path.

Find the Python that runs your Hermes:

```bash
find $HOME/.hermes $HOME/hermes-agent $HOME/.local -name python -path '*/venv/*' 2>/dev/null
```

Then re-run with the override. **The env var must be on the RIGHT side of the pipe** so it applies to `bash`, not to `curl`:

```bash
curl -fsSL https://cdn.autonomous.ai/os/tools/setup-remote-hermes.sh | \
  HERMES_PY=/full/path/to/venv/bin/python bash
```

Same pattern for `HERMES_BIN` when the `hermes` CLI isn't on `PATH`, or when the auto-detected CLI is the wrong one:

```bash
curl -fsSL https://cdn.autonomous.ai/os/tools/setup-remote-hermes.sh | \
  HERMES_BIN=/full/path/to/hermes bash
```

### The Mac's firewall blocks incoming connections
On macOS: **System Settings → Network → Firewall**. Either turn the firewall off for testing, or add an "allow incoming" rule for the `hermes` binary (the CLI at `~/.grid/tools/hermes-agent/bin/hermes` on grid installs).

### The chat works but stops after Mac sleeps
`hermes gateway run` in the foreground exits when the Mac sleeps. Install as a background service so it survives sleep and reboot:

```bash
hermes gateway install
```

Then run the setup script again — it will see the launchd service, skip the foreground restart, and just update the env.

---

## Env var overrides

The setup script honours these environment variables — most people don't need any of them:

| Variable        | Default            | Purpose                                                                 |
| --------------- | ------------------ | ----------------------------------------------------------------------- |
| `HERMES_ROOT`   | auto-detected      | Path to the Hermes install (folder containing `bin/python`).            |
| `HERMES_HOST`   | `0.0.0.0`          | Bind address for the API server. `127.0.0.1` limits it to the Mac only. |
| `HERMES_PORT`   | `8642`             | Bind port. Must match the device's expected port (default `8642`).      |
| `HERMES_TOKEN`  | random or existing | Force a specific API key instead of auto-generating one.                |

Example — force a specific token, in case you want the same one across three Macs:

```bash
HERMES_TOKEN='team-shared-key-2026' curl -fsSL https://cdn.autonomous.ai/os/tools/setup-remote-hermes.sh | bash
```

---

## Security notes

- The API server binds on `0.0.0.0` so any device on your LAN can hit it. Bearer-token auth is the only gate. **Choose a strong token** if your LAN isn't trusted (open cafés, guest Wi-Fi, etc.), and treat that token like a password.
- Requests dispatched through this endpoint run tool calls on the Mac with the same permissions as your user account (file access, shell commands, everything Hermes normally does). Only bind on `0.0.0.0` on networks you trust.
- The API key is stored plaintext in `~/.hermes/.env`. Anyone with read access to that file can talk to your Hermes.
- If you want stricter isolation, run the Mac's Hermes inside a Docker container with `terminal.backend: docker` — the Hermes docs cover that.

---

## Rolling back to the device's local agent

In the device UI, **Settings → Runtime** → pick any other backend (OpenClaw is the default). The Runtime settings page will restart os-server and the device is back to running its own brain locally.

The Mac-side Hermes stays running; you can leave it or run `hermes gateway stop` on the Mac to shut it down.
