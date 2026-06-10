# Hermes agent backend

Hermes is one of the **swappable agentic backends** the os-server can run behind
its agent gateway. The brain is pluggable (CLAUDE.md): os-server talks to
whatever backend `config.agent_runtime` selects through the single
`domain.AgentGateway` interface, so the rest of the pipeline (HAL TTS, `[HW:/…]`
hardware markers, Flow Monitor SSE, sensing drain, Telegram fan-out) never knows
which brain is active.

- **`openclaw`** (default): persistent WebSocket to the OpenClaw daemon. See `docs/os-server.md` + `internal/openclaw`.
- **`hermes`**: HTTP + SSE client against a local Hermes API server (OpenAI *Responses API* style). This doc. Code: `os/services/internal/hermes/`.

> Source of truth is the code. This documents `internal/hermes/` as implemented;
> keep it in sync on change (EN: this file, VI: `docs/vi/hermes_vi.md`).

## 1. When and how it is selected

`agent_runtime` in `config.json` picks the backend; resolution lives in
`internal/agent/factory.go` `ProvideGateway()`:

| `agent_runtime` | Backend |
|---|---|
| unset / `"openclaw"` | OpenClaw (default) |
| `"hermes"` | Hermes (`hermes.ProvideService`) |
| anything else | OpenClaw (logged as `FALLBACK — unknown agent_runtime=…`) |

On startup `ProvideGateway` prints an `AGENT BACKEND ACTIVE → HERMES` banner with
`base_url`, `conversation`, `model`, and `api_key_set`. There is **no per-unit
config** for these yet — they are compile-time constants in
`internal/hermes/constants.go`:

| Const | Default | Meaning |
|---|---|---|
| `BaseURL` | `http://127.0.0.1:8642` | Local Hermes API server |
| `APIKey` | `hermes-api-key` | Bearer for Hermes |
| `Conversation` | `lumi-main` | Named channel all turns flow into |
| `Model` | `hermes-agent` | Model id sent to Hermes |

Hermes itself is assumed to be already running on the device at `BaseURL` with
all skills provisioned; os-server is only a per-request client.

## 2. What changes vs OpenClaw — and what does not

| | OpenClaw | Hermes |
|---|---|---|
| Transport | one persistent WebSocket | stateless HTTP POST + SSE per turn |
| Connection state | socket up/down | `/health` poller goroutine (`health.go`) drives `ready`/`connectedAt` |
| Session | the socket | server-side UUID via `X-Hermes-Session-Id` header (§3) |
| Downstream pipeline | — | **identical** — Hermes translates SSE → the same `domain.WSEvent` frames |

Because Hermes emits the same `domain.WSEvent` shape that the OpenClaw handler
(`server/agent/delivery/http/handler_events.go`) already consumes, HAL TTS,
`[HW:/…]` marker routing, monitor SSE, the sensing drain, and Telegram fan-out
all stay untouched. `*hermes.Service` satisfies `domain.AgentGateway` in full
(`Name()`="Hermes", `IsReady`, `ConnectedAt`, `AgentUptime`, `IsBusy`/`SetBusy`,
`QueuePendingEvent`, `SendChat*`, `StartWS`, …).

## 3. Session & conversation model

Hermes has no socket, so the "session" is server-side:

- Every response carries the `X-Hermes-Session-Id` response header — one UUID per
  conversation, stable across reconnects. `Service.sessionUUID` shadows it.
- `Conversation` (`lumi-main`) is the named channel every turn flows into; all
  chat/sensing/Telegram turns share it so the agent keeps one context.
- `Service.lastResponseID` caches the latest `response.id`, used to chain turns
  (Responses-API style continuation).

State is in-memory only (`sessionUUID`, `lastResponseID`, `reqCounter` + the
guard / broadcast / web_chat / pose-bucket run trackers); nothing persists across
an os-server restart.

## 4. Request protocol — `POST /v1/responses`

`client.go` POSTs a `streamRequest` with `stream: true` and reads an SSE stream:

```jsonc
{
  "model": "hermes-agent",
  "conversation": "lumi-main",
  "stream": true,
  "instructions": "…",        // optional system/role text
  "input": "<text>",           // plain turn …
  "title": "…"                 // optional
}
```

For **vision** turns `input` is a multi-part array instead of a string — Hermes
accepts both shapes:

```jsonc
"input": [{ "role": "user", "content": [
  { "type": "input_text",  "text": "…" },
  { "type": "input_image", "image_url": "data:…" }
]}]
```

## 5. SSE → `domain.WSEvent` translation

The SSE consumer (`client.go`) streams `response.*` events; `translator.go` maps
them into `domain.WSEvent` frames and dispatches them through the handler
registered by `StartWS` — the same path OpenClaw uses. The turn-lifecycle mirror
matches OpenClaw: `activeTurn` flips true on send and false on
`response.completed`; the completed result carries `response.id` (cached as
`lastResponseID`) and the full assistant text for send-and-wait callers.

Sensing/pose markers are stripped before send using the same regexes as OpenClaw
(`[snapshot: …]`, `[pose_bucket: …]`, `[pose_worst: …]`) so the agent never sees
internal hardware markers.

## 6. Connection state & health

No socket means liveness is polled. `health.go` runs a `/health` poller that
flips `ready` / `connectedAt`, derives `agentStartedAt` from
`/health/detailed.uptime_s` when available, and uses `hasConnected` to skip the
"reconnected" TTS chime on the first successful poll. `AgentUptime()` reports the
Hermes process uptime, independent of os-server.

## 7. Busy state & pending sensing events

Identical contract to OpenClaw: while a turn is active (`IsBusy`), passive sensing
events are dropped or buffered (`QueuePendingEvent`, last-write-wins per type) and
replayed when idle, so ambient signals never interrupt an in-flight command.

## 8. Telegram fan-out

`telegram.go` / `telegram_sender.go` route agent replies back to the originating
Telegram chat. `markTelegramOrigin(runID, chatID)` records where a turn came from
and `consumeTelegramOrigin(runID)` reads it back at reply time, so a Telegram-
initiated turn answers in the right chat while still flowing through the shared
pipeline.

## 9. Voice

`hal.go` wires Hermes turns into the HAL voice path (TTS on speak-end, the same
`lib/hal` entry points OpenClaw uses), so spoken interaction works the same
regardless of backend.

## 10. Operating it

1. Run Hermes on the device at `http://127.0.0.1:8642` with skills provisioned.
2. Set `"agent_runtime": "hermes"` in `config.json`, restart os-server.
3. Confirm the `AGENT BACKEND ACTIVE → HERMES` banner + a healthy `/health` poll
   in the logs.

To target a different Hermes endpoint / key / model today, edit
`internal/hermes/constants.go` and rebuild (making these per-unit configurable is
future work).
