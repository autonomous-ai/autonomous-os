# PicoClaw agent backend

PicoClaw is one of the **swappable agentic backends** the os-server can run
behind its agent gateway. The brain is pluggable (CLAUDE.md): os-server talks to
whatever backend `config.agent_runtime` selects through the single
`domain.AgentGateway` interface, so the rest of the pipeline (HAL TTS, `[HW:/…]`
hardware markers, Flow Monitor SSE, sensing drain, Telegram fan-out) never knows
which brain is active.

- **`openclaw`** (default): persistent WebSocket to the OpenClaw daemon. See `docs/os-server.md` + `internal/openclaw`.
- **`hermes`**: HTTP + SSE client against a local Hermes API server. See `docs/agentic/hermes.md` + `internal/hermes`.
- **`picoclaw`**: persistent WebSocket client against a local PicoClaw runtime. This doc. Code: `os/services/internal/picoclaw/`.

> Source of truth is the code. This documents `internal/picoclaw/` as
> implemented; keep it in sync on change (EN: this file, VI: `docs/vi/agentic/picoclaw_vi.md`).

> **Agentic-backend docs:** [`adding-agent-runtime.md`](adding-agent-runtime.md)
> (generic contract + how to add one) · [`hermes.md`](hermes.md) (Hermes) ·
> this file (PicoClaw).
>
> **Status: client-only / incomplete.** PicoClaw is wired as a gateway *client*
> only — there is **no install, no presync, no persona/memory migration, no skill
> import/watch, no onboarding**. Everything beyond the WS hot path is a no-op (§8).
> Treat it as not-yet-at-parity; the `adding-agent-runtime.md` checklist is the gap
> list if it is ever brought up to a full backend.

## 1. When and how it is selected

`agent_runtime` in `config.json` picks the backend; resolution lives in
`internal/agent/factory.go` `ProvideGateway()`:

| `agent_runtime` | Backend |
|---|---|
| `"openclaw"` / unset | OpenClaw (default; or `gateway.default` from `DEVICE.md`) |
| `"hermes"` | Hermes (`hermes.ProvideService`) |
| `"picoclaw"` | PicoClaw (`picoclaw.ProvideService`) |
| anything else | OpenClaw (logged as `FALLBACK — unknown runtime=…`) |

On startup `ProvideGateway` prints an `AGENT BACKEND ACTIVE → PICOCLAW` banner
with `ws_url`, `conversation`, and `source`.

### Onboarding / install is out of scope here

Unlike OpenClaw and Hermes, this backend assumes **PicoClaw is already running**
on the device as a systemd service exposing its WebSocket. os-server is only a
**client** — there is no `install.sh`, no `runtimereg` registration, and no
config seeding. The `picoclaw.setup` MQTT switch + `switch-runtime` flow that
flips `config.agent_runtime` already exist (`server/device/delivery/mqtt/`,
`internal/device/switch_runtime.sh`); provisioning the PicoClaw service itself is
handled out of band.

## 2. Wire constants

There is **no per-unit config**; the endpoint is a compile-time constant in
`internal/picoclaw/constants.go`:

| Const | Default | Meaning |
|---|---|---|
| `WSURL` | `ws://127.0.0.1:18790/pico/ws/` | Local PicoClaw WebSocket endpoint |
| `Token` | `darren_pico_token` | Bearer token sent in the `Authorization` header on connect |
| `Conversation` | `device-main` | Default session label until the server assigns a `session_id` |

## 3. Transport

`client.go` holds **one persistent WebSocket** (gorilla/websocket), mirroring the
openclaw reconnect loop but simplified — PicoClaw has **no challenge / pairing
handshake**, just a bearer token:

1. `StartWS` dials `WSURL` with `Authorization: Bearer <Token>`.
2. On connect, readiness flips true (`IsReady`/`ConnectedAt`), the `StateAgentDown`
   LED clears, and a reconnect (not first-connect) plays the i18n reconnect TTS.
3. A keepalive goroutine sends `{"type":"ping","id":…}` every 25s; PicoClaw replies
   `pong` (ignored) which refreshes the 90s read deadline.
4. The read loop translates each inbound frame and dispatches into the registered
   `domain.AgentEventHandler` (synchronously — safe because `FetchChatHistory` is a
   no-op here, so the handler never blocks on a WS RPC).
5. On drop: clear busy + in-flight turn ids, paint `StateAgentDown`, stop servo
   tracking (motion devices only), back off 5s, reconnect.

## 4. Sending a turn

`chat.go` `sendChat` writes one frame and returns immediately (the reply arrives
on the read loop):

```json
{ "type": "message.send", "id": "<reqID>", "payload": { "content": "<text>" }, "session_id": "<if known>" }
```

- Image turns add `payload.attachments: [{ "type": "image", "url": "data:image/jpeg;base64,…" }]` (best-effort; the text content is always sent so the turn proceeds even if the attachment shape is ignored).
- Before the write: mark busy, stash the `runID` as the **pending run id**, record a pending chat trace, and emit `chat_input` / `chat_send` flow events (parity with openclaw).

PicoClaw processes **one turn at a time** and does not stream tokens, so turns
are correlated by a single in-flight `runID` rather than a per-frame id: the
pending run id is adopted by the first inbound frame of the turn.

## 5. Inbound protocol → `domain.WSEvent` mapping

This is the critical part for correct Flow Monitor / web-chat rendering. The
frame `type` alone is **not** enough — `message.create` / `message.update` must
be classified by their payload (`placeholder` / `kind` / `tool_calls` / `content`),
in this priority order (`translator.go` `categorize`):

| Inbound frame | Classified as | Emitted `domain.WSEvent` |
|---|---|---|
| `typing.start` | turn start | `agent` lifecycle `phase:start` (once per turn) |
| `message.create/update`, `placeholder:true` | thinking | *(none — status, not content)* |
| `message.create/update`, `kind:"thought"` / `thought:true` | reasoning | *(none — rendered as status only)* |
| `message.create`, `kind:"tool_calls"` / has `tool_calls` | tool call | `agent` tool `phase:start` + `phase:end` per call |
| `message.create/update`, non-empty `content` (none of the above) | **final answer** | `chat` `state:final role:assistant` **+** `agent` lifecycle `phase:end` (with usage) — **ends the turn** |
| `error` | error | `agent` lifecycle `phase:error` — ends the turn |
| `typing.stop` / `message.delete` / `pong` | — | *(ignored)* |

### Turn lifecycle gotchas

- **`typing.stop` is NOT the end of the turn.** It arrives early, right after the
  thinking phase. The turn ends only on the first **final** frame (or `error`).
- **No-tool turn:** `typing.start → placeholder → typing.stop → message.update (final)`.
  The final is a `message.update` that reuses the placeholder's `message_id`.
- **Tool turn:** `placeholder → typing.stop → message.delete (placeholder removed)
  → message.create kind:"tool_calls" (×N) → message.create (clean, final)`.
- PicoClaw does not emit a separate tool-result frame, so each tool call emits a
  `tool` `phase:start` immediately followed by a `phase:end` with an empty result,
  purely to close the trace.
- `media.create` is defined in the protocol but the server never emits it — media
  rides inside `message.create` as `attachments`.

### Tool call shape

Each entry in `tool_calls` is OpenAI-style: name + params live in
`function.name` and `function.arguments` (a **JSON string**, not an object). The
agent's human-readable lead-in is in `extra_content.tool_feedback_explanation`
(may contain stray ANSI control chars from terminal input). The current
translator forwards `name` + `arguments`; the explanation is logged but not
surfaced (the device `AgentPayload` has no slot for it).

### Token usage

`context_usage` (only on the final frame) is cumulative context size, not
per-turn input/output. It maps to `TokenUsage{ InputTokens: history_tokens,
TotalTokens: used_tokens }`.

## 6. Session

PicoClaw owns the session: the server-assigned `session_id` is captured from any
inbound frame and stored (`SetSessionKey`) so the next `message.send` echoes it.
`NewSession` just clears the local id so the next turn starts a fresh server
session. There is no compact RPC, so `CompactSession` is a no-op.

## 7. Channel capability

PicoClaw runs **telegram only**. The Telegram receive loop is **device-owned**
(driven by `config.TelegramBotToken`), and PicoClaw has no slack/discord delivery
of its own. The three channel methods in `internal/picoclaw/channels.go` encode
this honestly:

| Method | telegram | slack / discord / whatsapp |
|---|---|---|
| `SupportedChannels()` | returns `[telegram]` (the only entry) | — |
| `AddChannel(…)` | honest **no-op success** — telegram is device-owned, so there is nothing to write into the runtime | returns `domain.ErrChannelNotSupported` |
| `RefreshChannelConfig(…)` | `("", nil)` — success no-op (no runtime re-apply needed) | returns `domain.ErrChannelNotSupported` |

This is part of a **repo-wide generic capability model**: every runtime declares
`SupportedChannels()` and returns `domain.ErrChannelNotSupported` (the string
`"channel_not_supported"`) for channels it cannot run, instead of the old silent
no-op. The shared not-supported behavior and the post-switch `ChannelReconcile`
are documented in [`adding-agent-runtime.md`](adding-agent-runtime.md) — see there
rather than duplicating here.

**Switching FROM openclaw → picoclaw:** if openclaw had slack/discord configured,
those channels become unsupported under PicoClaw. After the switch
`ChannelReconcile` reports them in the MQTT info uplink's `unsupported_channels`
field (`domain.MQTTInfoResponse`), and their creds **stay in `config.json`** —
switching back to openclaw restores them.

## 8. What is stubbed

Everything not on the PicoClaw hot path is a no-op so the single
`domain.AgentGateway` interface is satisfied without inventing features the
backend does not have: `SetupAgent`, WhatsApp pairing, `ResetAgent`,
`RestartAgent`, `RefreshModelsConfig`,
`EnsureOnboarding`, `FetchChatHistory`, `GetConfigJSON`, MCP entry writes,
`WatchIdentity`, `UpdateIdentityName`, skill/model watchers, `UpdatePrimaryModel`.
HAL TTS/voice, Telegram fan-out, sensing-event queue/drain, and the run-marker
helpers (guard / broadcast / web-chat / silent / pose-bucket) are backend-agnostic
and behave exactly like the Hermes backend.
