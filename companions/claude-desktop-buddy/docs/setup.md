# Setup, Deployment & Troubleshooting

How to build `buddy-plugin`, get it onto a device, configure it, pair it with
Claude Desktop, and recover when BLE misbehaves.

## Build

From the **repo root** (not this folder):

```bash
make buddy-build
# → cd claude-desktop-buddy && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w" -o buddy-plugin .
```

Produces a static `linux/arm64` binary `claude-desktop-buddy/buddy-plugin` for the
Raspberry Pi / OrangePi target. The build uses the vendored BLE fork via the
`replace` directive in `go.mod` — no extra setup needed.

> Per repo policy the binary is **not committed**; version is injected/shipped via
> OTA. `VERSION_BUDDY` holds the current version string.

## Deploy

Two paths, both already wired in `scripts/`:

- **First-time / full image** — `scripts/setup.sh` installs Buddy as part of device
  provisioning.
- **Standalone / update** — `scripts/setup-claude-desktop-buddy.sh` (run on the Pi
  as root) fetches the binary from OTA metadata and (re)installs the service.

Or push manually during development from the repo root:

```bash
make upload-claude-desktop-buddy     # scripts/upload-claude-desktop-buddy.sh
```

### On-device layout

| Path | What |
|------|------|
| `/opt/claude-desktop-buddy/buddy-plugin` | the binary |
| `/opt/claude-desktop-buddy/VERSION_BUDDY` | installed version |
| `/opt/claude-desktop-buddy/chars/` | folders pushed from Claude Desktop |
| `/root/config/buddy.json` | runtime config (not overwritten on update) |
| `/var/lib/claude-desktop-buddy/stats.json` | lifetime approval/denial counters |
| `/var/log/claude-desktop-buddy.log` | rotating log (2 MB × 10) |
| `/etc/systemd/system/claude-desktop-buddy.service` | the unit |

### systemd unit

```ini
[Unit]
Description=Lamp Claude Desktop Buddy (BLE)
After=bluetooth.target lamp.service
Wants=bluetooth.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/claude-desktop-buddy
ExecStart=/opt/claude-desktop-buddy/buddy-plugin -config /root/config/buddy.json
Restart=always
RestartSec=5
SyslogIdentifier=claude-desktop-buddy
```

Runs as **root** (needs the system D-Bus for the BlueZ agent and the debugfs knobs
for advertising tuning). Starts after `bluetooth.target` and `lamp.service`.

```bash
sudo systemctl enable  claude-desktop-buddy
sudo systemctl restart claude-desktop-buddy
sudo systemctl status  claude-desktop-buddy
journalctl -u claude-desktop-buddy -f
```

### OTA

`setup-claude-desktop-buddy.sh` reads the `claude-desktop-buddy` entry
(`version`, `url`) from the OTA metadata JSON
(`https://storage.googleapis.com/.../lamp/ota/metadata.json`) and installs the
matching binary. Bumping the version there is what ships an update to the fleet.

## Configuration

`config/buddy.json` (deployed to `/root/config/buddy.json`). Flags:
`-config <path>` (default `/root/config/buddy.json`), `-log <path>`.

```json
{
  "enabled": true,
  "device_name": "Claude-lamp-{MAC}",
  "http_port": 5002,
  "lelamp_url": "http://127.0.0.1:5001",
  "lamp_url": "http://127.0.0.1:5000",
  "approval_timeout_sec": 30,
  "narration_lang": "vi"
}
```

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | if `false`, the process logs and exits immediately |
| `device_name` | `Claude-lamp-{MAC}` | BLE advertised name; `{MAC}` → last 4 hex from Lamp's `/api/system/network`, lowercased to match `lamp-xxxx.local` |
| `http_port` | `5002` | Buddy's local HTTP API port |
| `lelamp_url` | `http://127.0.0.1:5001` | LeLamp hardware runtime base URL |
| `lamp_url` | `http://127.0.0.1:5000` | Lamp Go API base URL (monitor/sensing buses, MAC lookup) |
| `narration_lang` | `vi` | TTS narration language (`en`/`vi`; unknown → English) |

> **Ignored / inert keys:** `led_mapping` (if present) is **not read** — it isn't in
> the `Config` struct; LED behavior is hardcoded in `bridge.go`. `approval_timeout_sec`
> is parsed but **unused** — there is no server-side approval timeout today.

Missing config file → defaults are used (with a log line). Parse error → defaults.

## Pairing with Claude Desktop

1. Make sure the service is running and advertising
   (`journalctl -u claude-desktop-buddy -f` shows `BLE advertising started`).
2. In Claude Desktop's **Hardware Buddy** picker, select the device by its
   advertised name (`Claude-lamp-xxxx`).
3. If a passkey is requested, read it from the journal:

   ```
   [agent] PAIRING PASSKEY for <device>: 123456 (entered 0/6)
   ```

   The device is a **DisplayOnly** BlueZ agent (it has no screen of its own), so it
   logs the 6-digit passkey; type that into Claude Desktop's prompt.

> The link is currently **unencrypted** (secure-only GATT flags are dropped at
> runtime because the Mac client doesn't auto-initiate SMP). `status.sec` reports
> `false`. Bonding will be re-enabled once Desktop initiates the handshake.

## Troubleshooting

### Device not discoverable / stopped advertising

Bounce the Bluetooth stack and the service together:

```bash
sudo systemctl stop claude-desktop-buddy
sudo systemctl restart bluetooth
sleep 3
sudo bluetoothctl power on
sudo systemctl start claude-desktop-buddy
```

### Slow discovery (Mac rarely sees it)

The journal shows `WARN: bluetooth debugfs not available — using BlueZ default
1280ms advertising`. Advertising is too slow for macOS scan windows. debugfs must
be mounted and the process must run as root for `tuneAdvIntervals()` to lower the
interval to 100–200 ms. Confirm `/sys/kernel/debug/bluetooth/hci*` exists.

### "No response" in the Hardware Buddy panel

Usually a characteristic-permission / bonding mismatch. The current build
deliberately drops the secure-only flags so the chars are reachable over the
unencrypted link — if you re-enable them, Desktop must complete LE Secure
Connections bonding first or reads/writes will fail with "No response".

### Dropped / corrupted BLE messages

Logs like `dropped N-byte BLE message (truncated)` are expected occasionally —
Write-Without-Response has no ACK so BlueZ drops packets under load. Single
heartbeats are harmless; a drop mid-transfer aborts the folder push (it must be
re-sent).

### MAC resolves to `unk`

`WARN: failed to fetch mac ...` or `mac is empty` means Lamp's
`/api/system/network` wasn't reachable/ready (Buddy retries ~15× / 2 s). The
device still runs, advertising as `Claude-lamp-unk`. Ensure `lamp.service` is up.

### Useful checks

```bash
curl -s http://127.0.0.1:5002/health    # ble_advertising + uptime
curl -s http://127.0.0.1:5002/status    # state, connected, pending_prompt
bluetoothctl show                       # adapter powered + discoverable
```
