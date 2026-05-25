# twitch-chat-hook

Drop-in Go reference for hooking Twitch live-stream chat messages into a backend via EventSub webhooks. Stdlib only — no third-party deps.

> **New devs should read [`HANDOFF.md`](./HANDOFF.md) first** — full end-to-end setup, current blocker, token refresh, production checklist.

## What it gives you

- `twitch/` — types, HMAC signature verification, minimal Helix client (token, subscribe, list, delete, user lookup).
- `cmd/webhook/` — HTTPS webhook receiver. Verifies signature, handles the challenge handshake, dedupes redeliveries, dispatches `channel.chat.message` events to `handleChatMessage`.
- `cmd/subscribe/` — one-shot CLI that creates the subscription against Helix.
- `cmd/irc/` — anonymous IRC reader fallback. No app / token / 2FA required. Use while the EventSub path is blocked on Developer Console access; same output format as `handleChatMessage`. See [Fallback: anonymous IRC](#fallback-anonymous-irc).

Copy `twitch/` into your BE module, then wire `handleChatMessage` to whatever you actually want (queue, DB, service call).

## Prereqs

1. **Register a Twitch app** at https://dev.twitch.tv/console/apps → get Client ID + Client Secret.
2. **OAuth user token for the bot account** with scope `user:read:chat`. Easiest path for one bot: Authorization Code flow once, save refresh token. The bot user also needs either:
   - `channel:bot` scope granted by the broadcaster, OR
   - to be a moderator in the channel.
3. **Public HTTPS endpoint** for the callback. Twitch will not subscribe to plain HTTP. For local dev use ngrok / cloudflared.
4. **Webhook secret** — random string 10-100 chars. Same value in `subscribe` and `webhook` processes.

## Run

```bash
cp .env.example .env
# fill in values, then:
set -a; source .env; set +a

# 1) start the webhook server
go run ./cmd/webhook
# in another shell, expose it: ngrok http 8080

# 2) create the subscription (Twitch will fire one verification POST)
go run ./cmd/subscribe \
  -channel  yourchannel \
  -bot      yourbot \
  -callback https://your-public-host/twitch/webhook
```

After subscribe, the webhook log should show `verified subscription <id>`. Then every chat message on `yourchannel` lands in `handleChatMessage`.

## Important behaviors

- **Signature**: `Twitch-Eventsub-Message-Signature = sha256=HMAC(secret, id || timestamp || body)`. We verify on the **raw** body before JSON-decoding. If you put this behind a framework, make sure the framework hands you the untouched bytes.
- **Replay protection**: requests older than 10 min are rejected (Twitch recommendation).
- **Idempotency**: in-memory dedupe by `Twitch-Eventsub-Message-Id`. For multi-instance deploys swap for Redis SETNX with TTL — see `dedupe` in `cmd/webhook/main.go`.
- **Always 2xx fast**. The handler ACKs as soon as the event is parsed and offloads work. Returning non-2xx triggers Twitch retries (up to 5 attempts with backoff).
- **Revocations**: if Twitch revokes (token expired, user removed scope, channel banned the bot, etc.) you get a `revocation` message — log it and re-subscribe after fixing.

## Limits

- Webhook subscription cost: `channel.chat.message` is **0 cost** per user — but each `(broadcaster, user)` pair counts once. App total cap is ~10,000 subscriptions.
- Rate limit on `POST /eventsub/subscriptions`: 300 creates/min per client.

## Alternative transport: WebSocket

If your bot runs on a user machine (no public HTTPS), use EventSub over WebSocket instead — same subscription type, same event shape, different transport. Open `wss://eventsub.wss.twitch.tv/ws`, take the `session_id` from the welcome frame, then call `POST /eventsub/subscriptions` with `transport.method=websocket` and `transport.session_id=<id>`. The HMAC verification code in `twitch/verify.go` is not used in that path — events arrive over the same WS.

## Fallback: anonymous IRC

If you cannot reach the Developer Console (e.g. 2FA blocked, account region rejected) and just need chat text flowing into your code, use `cmd/irc`. It connects to Twitch's legacy IRC gateway with an anonymous `justinfan<digits>` nick — no app, no token, no 2FA — and prints the same `[twitch-chat] #<channel> <<user>> <text>` line that `handleChatMessage` produces.

```bash
go run ./cmd/irc -channel <broadcaster_login>
# multi-channel: -channel foo,bar,baz
```

Caveats:
- Read-only. Cannot send messages.
- No HMAC, no `message_id`, no event metadata (cheer/reply/badges). If you need any of that, you need EventSub.
- Twitch has announced IRC deprecation. Treat this as a short-term workaround, not a permanent transport.

## Files

```
twitch-chat-hook/
├── go.mod
├── .env.example
├── README.md
├── cmd/
│   ├── webhook/main.go      HTTPS webhook receiver
│   ├── subscribe/main.go    create subscription CLI
│   └── irc/main.go          anonymous IRC fallback (no app required)
└── twitch/
    ├── types.go             EventSub payload types
    ├── verify.go            HMAC SHA256 verification
    └── client.go            Helix client (subscribe / list / delete / users)
```

## References

- EventSub overview — https://dev.twitch.tv/docs/eventsub/
- `channel.chat.message` schema — https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/#channelchatmessage
- Handling webhook events (signature, retries) — https://dev.twitch.tv/docs/eventsub/handling-webhook-events/
- Chat authentication & scopes — https://dev.twitch.tv/docs/chat/authenticating/
