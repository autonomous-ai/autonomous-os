# Device-resident Intern bridge

`runtimes/intern/bridge` implements protocol `0.2.0`, schema `cassi-first.v1`,
inside the existing Go `os-server` binary. The bridge provides text drafting
and proposed reception metadata, never employee execution or hardware control.
The owning Intern service now has an optional authenticated service-dispatch
seam, disabled by default; the bridge itself never dispatches. Only McAvoy and
PAM proposals can reach the canonical substrate authority when explicitly
configured. Receipts acknowledge acceptance/queueing with `delivered=false`.
See [configuration and response contract](intern-bridge-client.md#staged-authenticated-service-dispatch-2026-09-15).

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

### Explicit larger-model configuration on GUS

The [Intern v2 hardware](../../robots/intern-v2/README.md#hardware) declares
4 GB RAM. Keep the device defaults above; a model verified on the GUS Mac is
not evidence of compatibility with Intern hardware. The existing 4B default
also requires a separate memory and inference check on the target device.

For an Intern `os-server` instance running **on the GUS Mac itself**, these
existing startup configuration fields select the Director-reported verified
GUS Ollama endpoint and larger model explicitly:

```json
{
  "intern_provider": "ollama",
  "intern_ollama_url": "http://127.0.0.1:11435",
  "intern_ollama_model": "qwen3:14b"
}
```

These are fields for that instance's existing configuration, not a replacement
configuration file or device defaults. Loopback refers to the host running
`os-server`. This does **not** configure a remote Intern device to reach GUS:
the current Ollama provider rejects non-loopback hosts. A device-to-GUS
inference route requires a separate design and compatibility verification;
none is implemented here. No network fallback or credentials are added.
`think:false`, `keep_alive:5m`, the single-call limit, and the existing
one-model node constraints remain unchanged. This example performs no
deployment, model provisioning, or service-dispatch configuration.

See the [compatibility receipt](../receipts/intern-local-inference-defaults-2026-09-15.md).

## Admission and routing

### Final transcript admission

The custom Intern OS server exposes
`POST /api/agent/intern/voice/transcript` beside the existing voice grant,
revoke, and chat endpoints. It is a contract for a trusted native producer; it
does not connect a microphone, HAL, TTS, or a device event path. The request
must use the same signed administrator bearer session that owns an active
ephemeral voice grant. Its exact JSON shape is:

```json
{
  "transcript": "Please welcome today's visitor.",
  "final": true,
  "wake_word": "Gus",
  "operation": "reception",
  "data_class": "business",
  "admission": "administrator_final_transcript"
}
```

Only final transcripts addressed by the exact `Gus` wake word
(case-insensitive) are admitted. `Rex`, `PAM`, `Cassi`, and `Melvil` are not
wake words for this endpoint, and acoustic wrappers such as `Hey Gus` are not
accepted. Operation must be `reception`; data class must be `public` or
`business`; and the administrator assertion must match exactly. Empty,
oversized, unknown-field, duplicate-field, interim, self-authorizing
restricted/secret-access, and otherwise invalid requests fail before queue or
provider access. A valid request uses the existing bounded Intern queue and
returns HTTP 202 with `run_id`, `state:queued`, and `scope:bridge_request`, the
same completion contract as voice chat. It never dispatches, calls HAL/TTS, or
logs the transcript.

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
one company wake and one internal address for routing. Explicit internal names
remain deterministic proposals; `Gus` alone identifies the company. No recursive
routing or fan-out. `route` remains model-free, with unknown intent and null
handoff for unresolved input.
Before inference, bounded service phrases route without classification: news,
headlines, and briefing propose McAvoy; notify/notification, alarm, and
remind/reminder propose PAM. Any smart-home, device, light, scene, fan, Hue,
Nanoleaf, Kasa, or home-control phrase overrides them to a custody hold; mixed
service/home text is therefore held. `classify` returns only
`question`, `draft`, `action` or `unknown`; these labels never authorize action.
The model receives the exact admitted text and one fixed system instruction,
without history, attachments, channel context, files or employee memory.

### Ordinary-language reception

For `reception`, unresolved public/business text, with or without a `Gus` wake,
may make **one** classification call to the existing local Ollama provider.
Explicit internal names, deterministic service routes, and custody holds do not
call the model. Mixed content/PAM service phrases (including a leading service
alias) return clarification without inference. An empty company-wake prompt
returns `needs_input`. Caller data classification is never inferred or upgraded.

The only accepted labels are `orchestration`, `engineering`, `service`,
`notification`, `alarm`, `reminder`, `library`, `news`, `briefing`, `smart-home`,
`reception`, and `unknown`, matching the bounded Python reception label contract.
After trimming surrounding whitespace, exactly one label must remain; JSON,
lists, persona addresses, explanations, and unknown labels are rejected. The
fixed instruction requires `unknown` for ambiguous or multiple destinations.
Labels map to the existing destinations above and produce at most one proposal.
`smart-home`/`reception` classifications return `custody_hold`, without output or
handoff. Existing home-keyword and caller-custody holds still run before inference.

For example, synthetic-provider tests classify “Give me a rundown to start the
day” as `briefing`, “What happened in the world today?” as `news`, “Wake me at
seven” as `alarm`, and “Let me know when the report is ready” as `notification`.
These are contract fixtures, not measurements of a live model's accuracy.

Provider absence/failure, malformed or ambiguous output, and `unknown` return
`reception_route`, intent `unknown`, null handoff, and fixed “Could you clarify?”
text. Successful proposals also use fixed review text: raw classification output,
reasoning, and provider errors never become user-facing prose. All existing
final-answer, token-ratio, timeout, response-size and concurrency checks apply.
Reception never makes a second generation call.

Explicit Cerebras selection keeps its existing `generate`/`classify` behavior.
This increment adds **local-only** reception classification: unresolved reception
under Cerebras returns clarification without network I/O or a provider switch.
Protocol `0.2.0`, schema `cassi-first.v1`, Cassi-first metadata, `executed:false`,
and `next_step:safe_escalation` are unchanged; no dispatch or execution is added.

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
- Invalid configuration fails startup. For `generate`/`classify`, provider absence, timeout, rejection,
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
See the [original verification receipt](../receipts/intern-native-bridge-2026-09-14.md)
and [ordinary-language reception receipt](../receipts/intern-reception-classification-2026-09-15.md).
