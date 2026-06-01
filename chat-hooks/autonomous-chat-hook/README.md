# autonomous-chat-hook

MQTT subscriber that bridges the Autonomous web chat UI into Lamp's sensing
pipeline. Standalone Go process, same shape as `twitch-chat-hook/`.

## Flow

```
Autonomous web user
        │ HTTP
        ▼
  Autonomous BE  ──MQTT publish──▶  broker  ──MQTT push──▶  cmd/mqtt
                                                              │
                                                              │ HTTP POST
                                                              ▼
                                          lamp /api/sensing/event
```

BE publishes a JSON payload to a topic the lamp's hook subscribes to:

```json
{"user": "alice", "text": "hello lamp"}
```

The hook forwards each message to the lamp's local sensing endpoint as a
synthesized voice event, prefixed with `[source: autonomous_web, user: <name>]`
so SOUL.md can distinguish web chat from a real spoken turn.

## Run

```bash
cp .env.example .env
# fill broker URL, creds, topic, ...
set -a; source .env; set +a
go run ./cmd/mqtt
```

## Config (env)

| Var | Required | Default | Notes |
|-----|----------|---------|-------|
| `AUTONOMOUS_MQTT_URL`       | yes | — | `tls://host:8883` or `tcp://host:1883` |
| `AUTONOMOUS_MQTT_TOPIC`     | yes | — | Topic filter, e.g. `lamp/+/chat` |
| `AUTONOMOUS_MQTT_USERNAME`  | no  | — | Broker auth |
| `AUTONOMOUS_MQTT_PASSWORD`  | no  | — | Broker auth |
| `AUTONOMOUS_MQTT_CLIENT_ID` | no  | `autonomous-chat-<hex>` | |
| `LAMP_SENSING_URL`          | no  | `http://127.0.0.1:5000/api/sensing/event` | |
| `AUTONOMOUS_SENSING_TYPE`   | no  | `voice` | Sensing event type |

## Layout

```
autonomous-chat-hook/
├── cmd/mqtt/main.go        # subscriber main
├── autonomous/forward.go   # HTTP POST to lamp sensing endpoint
├── .env.example
└── VERSION_AUTONOMOUS_CHAT
```

## Deploy (Pi)

Build + upload from repo root (mirrors `make upload-twitch-irc`):

```bash
make upload-autonomous-chat        # bumps VERSION_AUTONOMOUS_CHAT, cross-compiles, zips, uploads to GCS, updates metadata.json key `autonomous-chat`
```

On the Pi, mirror lelamp's `EnvironmentFile=/opt/<component>/.env` convention:

```
/opt/autonomous-chat-hook/
├── autonomous-chat   # binary pulled from OTA zip
└── .env              # copy of .env.example, filled with broker creds
```

Systemd unit (suggested, not yet baked into imager):

```ini
[Unit]
Description=Autonomous chat MQTT bridge → lamp sensing
After=network-online.target lamp.service
Wants=network-online.target

[Service]
EnvironmentFile=/opt/autonomous-chat-hook/.env
ExecStart=/opt/autonomous-chat-hook/autonomous-chat
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

## Notes

- Uses `github.com/eclipse/paho.golang/autopaho` for auto-reconnect with
  exponential backoff — same library lamp's `lib/mqtt` uses.
- Fire-and-forget HTTP forward: slow lamp never blocks the MQTT receive loop.
- Empty `user` field is forwarded as `anonymous`.
- Sibling reference impl: `twitch-chat-hook/` (IRC-based, same `forward` shape).
