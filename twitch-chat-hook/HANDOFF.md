# Handoff — twitch-chat-hook

For the developer taking this over. Read this end-to-end before touching anything else.

---

## 1. Goal

Pipe every chat message from one (or more) Twitch live-stream channels into the BE for downstream processing — logging, queueing, feeding into the AI pipeline, etc.

Approach: **Twitch EventSub Webhook** with subscription type `channel.chat.message`. Twitch POSTs each message to a public HTTPS endpoint, signed with HMAC-SHA256 using a shared secret.

We are **not** using IRC (legacy; Twitch recommends migrating away). We are **not** using the WebSocket transport (only suitable for bots running on a user machine without a public HTTPS endpoint).

---

## 2. Current status

| Item | Status |
|---|---|
| Prototype code (`twitch-chat-hook/`) | **Done** — builds clean, stdlib only |
| Twitch Developer Console app registration | **Blocked — not done yet** |
| OAuth bot user token | **Not obtained** (needs 2FA → needs registered app → needs Twitch console) |
| Deploy into real BE | **Pending** (waiting on token) |

### Blocker: cannot enable 2FA on Twitch

- The Developer Console (`https://dev.twitch.tv/console/apps`) requires the account to have 2FA enabled before you can register an app.
- Enabling 2FA requires a verified phone number.
- Vietnamese mobile numbers (Viettel `+84976…`) are rejected at the "Add phone number" step with error `INVALID_PHONE_NUMBER`. This is Twitch's fraud filter, not a format issue — the E.164 format is correct.

**Three options to unblock** (needs product / PM call):

1. **Use a different number** — a postpaid number from a major carrier that has never been attached to any Twitch account. Once 2FA is bootstrapped via SMS, an authenticator app works independently and the phone number can be discarded.
2. **Contact Twitch Support** — https://help.twitch.tv/s/contactsupport, request whitelisting the VN number. Include the `requestID` from the failed network request (visible in DevTools → Network). Turnaround is unpredictable, typically 1-2 weeks.
3. **Use a different Twitch account** that already has 2FA enabled (e.g. a rented / purchased account). Risky vs. TOS, but fast.

### Once unblocked

Everything else is pure technical work — code and instructions are below.

---

## 3. End-to-end setup

### Step 1 — Register a Twitch app

1. Sign in with a Twitch account that has 2FA enabled → go to https://dev.twitch.tv/console/apps
2. Click **Register Your Application**
3. Fill in the form:
   - **Name**: `<your-app-name>` (must be globally unique on Twitch)
   - **OAuth Redirect URLs**: `http://localhost:3000/callback` for dev, or your real BE URL for prod
   - **Category**: `Chat Bot`
   - **Client Type**: `Confidential` (the BE keeps the secret)
4. Click **Create** → the page reveals `Client ID` (public, safe to log) and `Client Secret` (private, store in a secret manager)

Save into the BE's secret manager / `.env`:
```
TWITCH_CLIENT_ID=<client_id>
TWITCH_CLIENT_SECRET=<client_secret>
```

### Step 2 — Obtain an OAuth user token for the bot

This is the trickiest part. `channel.chat.message` **requires a user token** (not an app token), with at least scope `user:read:chat`.

#### Easiest path: use the Twitch CLI

```bash
# Install (macOS)
brew install twitchdev/twitch/twitch-cli

# Configure with the Client ID / Secret from step 1
twitch configure
# it'll prompt for Client ID, Secret, and Redirect URL — paste them in

# Mint a user access token with the scopes we need
twitch token -u -s "user:read:chat user:bot"
```

The command opens a browser → sign in with the **bot account** (NOT a personal account!) → approve → the CLI prints:

```
User Access Token: oauth:abcd1234...
Refresh Token:    xyz9876...
Expires In:       14400 s
Scopes:           [user:read:chat user:bot]
```

Save both the access token and the refresh token:
```
TWITCH_BOT_USER_TOKEN=abcd1234...
TWITCH_BOT_REFRESH_TOKEN=xyz9876...
```

The access token is valid for 4 hours. After that, use the refresh token to renew it (see section **4. Token refresh**).

#### Important notes

- **Bot account ≠ personal account.** Create a separate Twitch account to act as the bot (e.g. `mybot_official`), and enable 2FA on it as well. The app can be owned by the personal account, but the authorization sign-in must be done as the bot account.
- The bot must have **one of two** authorizations for each broadcaster channel you want to hook:
  - Be a **moderator** in that channel (the broadcaster makes the bot a mod)
  - Or the broadcaster grants the `channel:bot` scope to the bot
- Verify the scopes immediately after obtaining the token:
  ```bash
  curl -H "Authorization: OAuth $TWITCH_BOT_USER_TOKEN" \
       https://id.twitch.tv/oauth2/validate
  ```
  You must see `scopes: ["user:read:chat", "user:bot"]` and the bot's `user_id`.

### Step 3 — Generate a webhook secret

```bash
openssl rand -hex 32
```

64 random hex characters. Save:
```
TWITCH_WEBHOOK_SECRET=<paste>
```

This secret is used in two places: when subscribing (Twitch stores it) and when verifying every incoming request (the BE recomputes the HMAC). The two must match.

### Step 4 — Deploy the webhook receiver

#### Local dev with ngrok

```bash
# Terminal 1
cd twitch-chat-hook
set -a; source .env; set +a
go run ./cmd/webhook
# listening on :8080

# Terminal 2
ngrok http 8080
# Forwarding: https://abc123.ngrok.app -> http://localhost:8080
```

Use `https://abc123.ngrok.app/twitch/webhook` as the callback URL.

#### Production

The code listens on plain HTTP `:8080`. **TLS must be terminated by a reverse proxy** (nginx / ALB / Caddy / Cloudflare). Twitch rejects plain-HTTP callbacks.

Exposed endpoints:
- `POST /twitch/webhook` — Twitch will POST here
- `GET /healthz` — health check for the load balancer

### Step 5 — Create the subscription

```bash
cd twitch-chat-hook
set -a; source .env; set +a

go run ./cmd/subscribe \
  -channel  <broadcaster_login> \
  -bot      <bot_login> \
  -callback https://your-host/twitch/webhook
```

Flow:
1. The CLI calls Helix `GET /users` to resolve `<broadcaster_login>` and `<bot_login>` into numeric user IDs.
2. The CLI calls Helix `POST /eventsub/subscriptions` with `type=channel.chat.message`, condition `{broadcaster_user_id, user_id}`, and a webhook transport carrying the callback URL and secret.
3. Twitch immediately POSTs a `webhook_callback_verification` request to the callback → the webhook server echoes the `challenge` field → the subscription flips to `enabled`.
4. The CLI prints `subscribed: id=<sub_id> status=webhook_callback_verification_pending`.
5. A few seconds later, the webhook server logs `verified subscription <sub_id>`.

Test: go to `<broadcaster_login>`'s chat, send any message → the BE log should immediately show:
```
[twitch-chat] #broadcaster_login <chatter_login> hello world
```

### Step 6 — Wire into the real BE

There is exactly one function to change: `handleChatMessage` in `cmd/webhook/main.go`.

```go
func handleChatMessage(ctx context.Context, ev twitch.ChatMessageEvent) error {
    // TODO: replace this with the real integration
    log.Printf("[twitch-chat] #%s <%s> %s",
        ev.BroadcasterUserLogin, ev.ChatterUserLogin, ev.Message.Text)
    return nil
}
```

Replace it with whatever you actually need — publish to a queue, call a service, persist to a DB. Keep these in mind:

- **Do not block.** This function runs within Twitch's retry timeout (10s). If the downstream is slow, push to a queue and return immediately.
- `ev.Message.Text` is already raw text (no markdown, no HTML). Emotes and mentions are broken out in `ev.Message.Fragments[]` if you need them.
- `ev.MessageID` is stable — use it as the idempotency key downstream.
- `ev.BroadcasterUserID` and `ev.ChatterUserID` are numeric — prefer these over `Login` for joins (logins can change).

When copying into the real BE module:
- Copy the `twitch/` folder (3 files: types / verify / client). Keep the package name `twitch` or rename per BE conventions.
- Move the logic of `cmd/webhook/main.go` into an HTTP handler in the existing HTTP layer. When unwrapping the body: **read the raw bytes BEFORE the framework parses them**, because the HMAC verifier needs the untouched bytes.
- Take `cmd/subscribe/main.go` out of the main binary — run it once during setup, or wire it into an admin command.

---

## 4. Token refresh

The bot's user token expires after 4 hours. Refresh must be automated; manual rotation is not viable.

```go
// pseudocode
func RefreshUserToken(ctx context.Context, refreshToken string) (newAccess, newRefresh string, expiresIn int, err error) {
    form := url.Values{
        "grant_type":    {"refresh_token"},
        "refresh_token": {refreshToken},
        "client_id":     {clientID},
        "client_secret": {clientSecret},
    }
    resp, _ := http.PostForm("https://id.twitch.tv/oauth2/token", form)
    // parse access_token, refresh_token, expires_in
}
```

Twitch returns a **new** `refresh_token` on each refresh — you must persist it. The old one is invalidated immediately.

Recommended pattern: a background goroutine that refreshes when there's ~30 minutes of TTL left, and persists the new token pair to the DB / secret store.

Note: the subscribe-only flow in this prototype does not need refresh, because once a subscription is created, Twitch signs each webhook with the stored secret and no user token is involved. Refresh is only needed for:
- Creating new subscriptions (e.g. adding a channel)
- Listing / deleting subscriptions
- Handling revocations (see section **5**)

---

## 5. Revocation handling

Twitch sends `Message-Type: revocation` when a subscription is cancelled. Common reasons:

| `subscription.status` | Cause | Fix |
|---|---|---|
| `authorization_revoked` | User token expired or the user changed password | Refresh the token, re-subscribe |
| `user_removed` | Bot account was banned from Twitch | Contact Twitch support |
| `notification_failures_exceeded` | BE returned non-2xx too many times | Fix the root cause, re-subscribe |
| `version_removed` | API version `1` deprecated | Check the Twitch changelog, update the code |

Current code only logs:
```go
case twitch.MsgTypeRevocation:
    log.Printf("[twitch-webhook] subscription revoked: id=%s status=%s type=%s", ...)
    w.WriteHeader(http.StatusNoContent)
```

**Production needs to add**:
- Alert / page on revocation
- Auto re-subscribe for recoverable statuses (`authorization_revoked` → refresh token → subscribe again)
- Backoff to avoid a re-sub loop

---

## 6. Production hardening checklist

The prototype is enough to run end-to-end. Before going to production:

- [ ] **Multi-instance idempotency**: replace the in-memory `dedupe` with `Redis SETNX msg_id TTL 10m`. The current code is fine for a single instance; multiple instances will double-process.
- [ ] **Metrics**: counters for — `verified_signature_fail`, `replay_rejected`, `dispatch_error`, `message_received`, `message_dispatched`. Histogram for dispatch latency.
- [ ] **Structured logging**: the prototype uses `log.Printf`. Swap for the BE's logger (zap/slog) with fields: `message_id`, `subscription_id`, `broadcaster_user_id`, `chatter_user_id`.
- [ ] **Downstream rate limiting**: a large broadcaster can peak at 100+ msg/s. Buffered queue + drop policy if needed.
- [ ] **Token refresh job**: background goroutine, secret store.
- [ ] **Subscription state DB**: store the `subscription_id` → `channel` mapping. Needed to re-subscribe (after a revocation) you need the channel list.
- [ ] **Delete dev-environment subscriptions on shutdown**: avoid zombie subscriptions polluting Helix.
- [ ] **Alert on revocation**.
- [ ] **TLS termination**: nginx / ALB / Caddy. Twitch requires a valid cert (Let's Encrypt is fine).

---

## 7. Test plan

### Unit (not yet written — please add)

- `twitch/verify.go` — valid signature, wrong secret, stale timestamp, missing headers
- `twitch/types.go` — unmarshal a sample `channel.chat.message` payload (from Twitch docs)

### Integration

1. **Verification handshake**: send a mocked `webhook_callback_verification` → server returns 200 + the challenge as plain text
2. **Valid notification**: HMAC signed correctly, body is `channel.chat.message` → dispatched
3. **Invalid signature**: signed with the wrong secret → server returns 403
4. **Replay**: timestamp 15 minutes in the past → server returns 403
5. **Duplicate**: same `Message-Id` twice → second one skips dispatch
6. **Revocation**: send a `revocation` type → log, return 204

### Manual E2E

In a dedicated test channel:
- [ ] Plain text chat → log shows the message
- [ ] Chat with emote (Kappa, etc.) → fragments parsed
- [ ] Reply to a message → `ev.Reply` is populated
- [ ] Cheer (Cheer100) → `ev.Cheer.Bits == 100`
- [ ] Channel-points custom-reward redemption with text → `ev.ChannelPts` is populated

---

## 8. Open questions for product / PM

1. **Multi-channel or single-channel?** The current code subscribes one channel per run. Multi-channel needs a loop and persistent state.
2. **Store chat long-term or just pipe through?** Affects DB schema and GDPR posture (Twitch user data).
3. **Retention policy**: Twitch does not give us a delete-API for messages we've already hooked. We must handle the case where a user deletes their message on Twitch ourselves.
4. **Max number of channels to hook?** Twitch caps the `(broadcaster, bot)` pair count per app. Scaling up requires a quota increase request.
5. **Do we need two-way chat** (the bot replying)? If yes, add scope `user:write:chat` and use `POST /chat/messages`.

---

## 9. References

- EventSub overview — https://dev.twitch.tv/docs/eventsub/
- `channel.chat.message` schema — https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/#channelchatmessage
- Handling webhook events (signature, retries) — https://dev.twitch.tv/docs/eventsub/handling-webhook-events/
- Chat authentication & scopes — https://dev.twitch.tv/docs/chat/authenticating/
- OAuth Authorization Code flow — https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#authorization-code-grant-flow
- Twitch CLI — https://dev.twitch.tv/docs/cli/
- Revocation reasons — https://dev.twitch.tv/docs/eventsub/handling-webhook-events/#revoking-your-subscription
