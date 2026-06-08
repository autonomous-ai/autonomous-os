# Claude Desktop Buddy

The on-device **Bluetooth bridge** between **Claude Desktop on the user's Mac**
(its "Hardware Buddy" feature) and the lamp.

A small Go daemon runs on the device (Raspberry Pi / OrangePi). Over a Bluetooth
LE [Nordic UART](https://learn.adafruit.com/introducing-the-adafruit-bluefruit-le-uart-friend/uart-service)
link it receives Claude Desktop's live activity — heartbeats, chat events, and
permission prompts — derives a simple **state** from them, and reflects that state
onto the lamp's **LED, display, and voice**. It also exposes a small HTTP API so
the on-device agent (OpenClaw) can read Desktop status and approve/deny tool calls
by voice.

> **Not to be confused with `lamp-buddy/`** — that is the *macOS* companion app
> (Swift) for remote computer use. **`claude-desktop-buddy/`** is the *device-side*
> Go plugin that pairs with Anthropic's Claude Desktop app over BLE.

```
 Claude Desktop (Mac)            Device (Pi / OrangePi)
 ┌───────────────────┐  BLE/NUS  ┌──────────────────────────────────────┐
 │  Hardware Buddy    │ ───────► │  buddy-plugin (this repo)             │
 │  • heartbeat       │  notify  │   BLE ─► state machine ─► bridge      │
 │  • chat events     │ ◄─────── │            │                 │        │
 │  • permission ask  │  ack /   │            ▼                 ▼        │
 └───────────────────┘  perm.    │      HTTP :5002        LeLamp :5001   │
                                  │   /status /approve     LED/display/  │
                                  │   /deny  (OpenClaw)    TTS · Lamp :5000│
                                  └──────────────────────────────────────┘
```

## What it does

- **State mirroring** — turns Desktop activity into the lamp's behavior:
  `idle`, `busy` (Claude is working), `attention` (waiting for your approval),
  plus brief `heart` / `celebrate` flourishes. See [docs](docs/architecture.md#state-machine).
- **Voice approvals** — when Claude Desktop asks permission for a tool call, the
  agent can ask you out loud and you answer by voice; Buddy relays the decision
  back over BLE. See [the skill](skill/SKILL.md).
- **Activity narration** — short, deduped TTS announcements ("Claude is editing a
  file", "Claude is done") so you can keep your eyes on the Mac.
- **Folder push** — receives a character/data folder streamed from Claude Desktop
  and writes it under `/opt/claude-desktop-buddy/chars`.

## Documentation

| Doc | What's inside |
|-----|---------------|
| [`docs/architecture.md`](docs/architecture.md) | Components, data flow, state machine, the LeLamp/Lamp bridge, narration, stats |
| [`docs/ble-protocol.md`](docs/ble-protocol.md) | Nordic UART wire format: framing, every message type, salvage, chunking, folder-push |
| [`docs/setup.md`](docs/setup.md) | Build, deploy (systemd / OTA), `buddy.json` config, BLE pairing, troubleshooting |
| [`skill/SKILL.md`](skill/SKILL.md) | The OpenClaw skill that drives voice approvals + state awareness |

Vietnamese versions live under [`docs/vi/`](docs/vi/).

## Quick reference

```bash
# Build (cross-compile to the device target)
make buddy-build            # from repo root → produces ./buddy-plugin (linux/arm64)

# Run (on the device)
./buddy-plugin -config /root/config/buddy.json -log /var/log/claude-desktop-buddy.log

# Service control (on the device)
sudo systemctl status  claude-desktop-buddy
sudo systemctl restart claude-desktop-buddy
journalctl -u claude-desktop-buddy -f      # watch logs (pairing passkey shows here)
```

**HTTP API** (default `:5002`, loopback): `GET /status`, `GET /health`,
`POST /approve`, `POST /deny`. Details in [`docs/architecture.md`](docs/architecture.md#the-approval-round-trip).

## Restart Bluetooth on the device

If the device stops advertising or Claude Desktop can't discover it, bounce the
Bluetooth stack and the service together:

```bash
sudo systemctl stop claude-desktop-buddy
sudo systemctl restart bluetooth
sleep 3
sudo bluetoothctl power on
sudo systemctl start claude-desktop-buddy
```

More failure modes — passkey pairing, slow discovery, "No response" in the
panel — are covered in [`docs/setup.md`](docs/setup.md#troubleshooting).
