# Device-resident Intern bridge

`runtimes/intern/bridge` implements protocol `0.2.0`, schema `cassi-first.v1`,
inside the existing Go `os-server` binary. `runtimes/intern.Service` and the
strict transport client are unchanged. This is text drafting and proposed
reception metadata, never employee execution, hardware control or dispatch.

## Lifecycle

Only the active Intern startup path creates the provider and binds
`tcp4 127.0.0.1:8765`. It binds before starting the queue worker. An occupied
port is an error, not permission to adopt another process. Failure to bind the
administrator listener also releases the bridge. No bridge process, Python,
Hermes, OpenClaw, installer, new service unit, channel or MQTT connection is
started. The existing `os-server.service` supervises the entire process.

SIGTERM/interrupt cancels the worker and provider request, drains HTTP requests
with a five-second shutdown budget per listener and forcibly closes remaining
sockets. Listener failure stops the Intern lifecycle. Selection changes still
require an operator-managed restart; changing the saved selection does not
start/stop this bridge in an already active different runtime.

## Configuration

Settings are read from the existing device `config/config.json` at startup.
This implementation never writes configuration, downloads a model, discovers
keys or queries another role/node. Restart is required to apply changes.

| Field | Default / purpose |
|---|---|
| `intern_provider` | Empty or `ollama`: device-local inference. Explicit `cerebras`: Cerebras HTTPS API. Other values, including `openai`, fail startup. |
| `intern_ollama_url` | `http://127.0.0.1:11434`; base origin only, no path except `/`. Only literal `127.0.0.1`, `::1` or `localhost` allowed. `localhost` becomes literal IPv4 before dialing. |
| `intern_ollama_model` | `qwen3:4b`; operator must provision the selected model separately. No availability claim or automatic pull. |
| `llm_base_url` | Used only for explicit remote provider selection; HTTPS API base such as `https://api.cerebras.ai/v1`. `/chat/completions` is appended. |
| `llm_model` | Required for explicit remote selection; no guessed remote model. |
| `llm_api_key` | Existing key, required only for explicit remote inference. Sent only as a bearer header to that endpoint, never to Ollama. |

Inherited general LLM settings cannot enable cloud inference. Missing/invalid
explicit remote configuration fails startup; it never switches to Ollama.
Choosing Cerebras explicitly opts this bridge into the existing `llm_base_url`,
`llm_model` and `llm_api_key` values. The model must support
`reasoning_effort:"none"`; unsupported models/options return a bounded fallback.
There is no automatic escalation from local Ollama to Cerebras on failure.
URLs containing userinfo, queries, fragments or escaped paths are rejected.
There is no environment/provider fallback, credential lookup or proxy discovery.
Configuration values, prompts, provider bodies and underlying errors are not logged
by the bridge. Errors are fixed sentinels; output echoing its remote key is rejected.

## Admission and routing

The administrator endpoint still requires the explicit classification of the
exact text. The transport client rejects unknown/restricted/secret data before
network I/O. The bridge repeats validation for direct callers before routing;
the provider repeats it before any inference. Only public/business may reach a
model. Labels are trusted caller policy, not a semantic privacy detector: a
mislabelled secret does not gain custody permission. The loopback endpoint is
for trusted local processes, not an authentication boundary against other local
users. Browser origins, credentials, cookies, forwarded headers, absolute-form
proxy requests and queries are rejected.

Every response first names `cassi@mama`, with `executes_actions:false`,
`executed:false`, `next_step:safe_escalation` and `lifecycle_scope:bridge_request`.
“Cassi” here is reception metadata; no connection to Mama is made.

| Leading name/intent after optional `Gus` wake | Proposed destination | Outcome |
|---|---|---|
| Gus / orchestration | `orchestration@gus` | Persona route or draft/classification |
| Rex / engineering | `rex@dru` | Persona route or draft/classification |
| Melvil / curator / library | `melvil@lab` | Persona route or draft/classification |
| PAM / service | `pam@gus` | `service_route`, no execution |
| Cassi / casi / cassandra / reception | `cassi@mama` | `custody_hold`, output absent, null handoff |
| news / briefing / daily briefing / morning briefing | `mcavoy@lab` | `service_route`, pending review, no news fetch |
| notification / alarm / reminder | `pam@gus` | `service_route`, pending review, no delivery or scheduling |
| smart-home / smart home | `smart-home` | `custody_hold`, output absent, null handoff |

Wake prefixes accept `hey`, `ok`, `okay` and `hey_gus`. The bridge strips at most
one company wake and one internal address for routing; the proposal is computed
once before inference and retained on completion. No recursive routing or
fan-out. `route`/`reception` use deterministic metadata only. Unaddressed input
gets unknown intent and null handoff; it is not guessed by a classifier.
Natural-language home intent detection is not claimed. `classify` returns only
`question`, `draft`, `action` or `unknown`; these labels never authorize action.
The model receives the exact admitted text and one fixed system instruction,
without history, attachments, channel context, files or employee memory.

## Completion and bounds

- One provider call at a time; concurrent model work fails unavailable, not queued.
- One request, no retry, redirect, proxy, cookies, alternate provider or fan-out.
- Ollama: `/api/chat`, `stream:false`, top-level `think:false`, `keep_alive:5m`,
  `num_predict:256`, temperature zero. Remote: `/chat/completions`,
  `max_completion_tokens:256`, `reasoning_effort:none`, temperature zero.
- Provider deadline 15 seconds including body reads; earlier caller cancellation
  wins. Connect/TLS timeout three seconds, response headers at most 8 KiB, body
  at most 64 KiB, one connection. TLS verification is enabled for remote calls.
- Only complete, non-streaming assistant content is accepted. Reasoning/tool
  payloads, unknown message fields, truncation, unsafe markers, duplicate JSON
  keys, invalid Unicode and trailing documents fail closed. Output is at most
  4,096 code points and never contains think/analysis/tool/HW markers. The
  filtering includes bracket/XML closing tags, reasoning headings and fenced
  labels, hardware-link syntax such as `[label](HW:/led/off)`, `<say>` wrappers
  and runtime sentinels. The entire response is rejected, never partially
  stripped into an apparent success. Ordinary final prose and Markdown remain
  supported; marker checks do not prove the semantic absence of unmarked reasoning.
  Ollama token counts must be 1–256 and no more than four times trimmed output code points;
  this conservative heuristic can reject legitimate terse responses and is not
  proof of a provider's internal reasoning behavior.
- Invalid configuration fails startup. Provider absence, timeout, rejection,
  unsupported options or unsafe response returns the existing fixed `fallback`
  response (`ErrUnavailable` in the client), with no provider error text.
- Input remains limited to 8,000 code points / 16 KiB JSON. Decoder rejects
  duplicate/unknown/case-variant fields, nulls and malformed Unicode.
- Supplied run IDs are SHA-256 hashed, reserved before inference, and replay
  returns 409. The bounded 4,096-ID process-lifetime ledger fails unavailable at
  capacity instead of evicting IDs. Restart resets it; this is not durable
  exactly-once execution. Omitted IDs get 128 random bits and are not retained.

`GET /health`, `/ready`, `/version` expose transport metadata only. No startup or
health probe performs inference. Gateway readiness requires a validated draft
and expires after 30 seconds. Cancellation stops the local HTTP wait; it cannot
prove a remote provider stopped computation or billing.

API references: [Ollama chat](https://docs.ollama.com/api/chat) and
[Cerebras chat completions](https://inference-docs.cerebras.ai/api-reference/chat-completions).
Models/providers that do not support the bounded final-answer request fail safely.
See the [verification receipt](../receipts/intern-native-bridge-2026-09-14.md).
