# Intern runtime registration assessment

## Full safe-registration recheck — 2026-09-14

OS source: `ed3e8b12d3b7890047f9d60bd075f1af89105d35`, existing PR #406.
Producer source inspected locally: project-spider-man Welcome Desk commit
`16bb0d3f17083ffc9c9b466ff7470e2e4807b083`, specifically
`packages/agents/src/gus/agents/intern_bridge.py`, `intern.py`, and
`packages/voice/src/gus/voice/welcome_desk.py`. This is source inspection,
not verification of a listener or installed model. The Go client remains pinned
to protocol `0.2.0` / `cassi-first.v1`.

**Decision: retain the unregistered client; do not make `intern` selectable.**
The decisive missing contract is who may admit a particular input and its
derived context as public/business. Neither an extra string field nor loopback
reachability establishes that authority. The current producer explicitly trusts
caller-supplied `data_class`; it does not authenticate or classify custody for
the OS. A server-wide business default, a prompt label, or a `web_chat` type
would therefore invent authority. Rejecting all ordinary input while permitting
only the fixed public probe would expose an unusable runtime as ready.

### Source-level findings and closure criteria

Line numbers below refer to the OS source commit above; symbols are the stable
lookup keys. These findings supplement the earlier audit with the actual
callers and ordering, rather than treating optional full-brain features as
requirements for a text-only Welcome Desk.

| Boundary | Source evidence | Required closure before registration |
|---|---|---|
| Trusted input admission | `system/server/server.go:450` attaches `sameOriginOrLAN` to sensing input. `system/server/middleware.go:22` checks location/origin, not an input classification authority. `SensingEventRequest` at `system/server/sensing/delivery/http/handler.go:89` has no data class or admission evidence; `PostEvent:211` binds caller JSON. | Identify an existing authorized producer/policy and verify its assertion before content use. A JSON `data_class` supplied by an arbitrary caller is insufficient. Unknown/restricted/secret and unsupported attachments must fail before logging, persistence, augmentation, or forwarding. No such authority is established by the inspected integration. |
| Data class across transformations and replay | `PostEvent:231` logs raw text; `:475` writes chat images; `:668` queues strings/images/ID. `:793` takes request/current mood identity, `:806` builds the augmented message, and `:834` can append Harness result context. The gateway send at `:905` receives only text and IDs. `runtimes/picoclaw/events.go:99` snapshots mood and `:252` rebuilds text on drain. | Carry immutable admission evidence bound to the actual payload through a bounded queue. Added identity, guard, environment, or Harness context needs its own admissibility decision; the class of the original text cannot authorize it. For Intern, bypass unsupported augmentation and reject images/files before their existing writes. Copying the existing queue is unsafe. |
| Completion and effects | `system/server/server.go:343` supplies the shared event handler to `StartWS`. `system/server/agent/delivery/http/handler_event_agent.go:335` sets busy on start and `:346` clears it on end/error. `:901` suppresses web-chat TTS, but `:936` still calls `fireHWCallsSync`. `system/server/config_watch.go:handleSetUpCompleteChange` independently starts reconciliation, token refresh, schedules, HAL setup, ambient behavior, and healthwatch. | A text-only event consumer and startup/config capability gates must precede these effects, with injected effect counters in tests. Client rejection of `[HW:` is useful but is not an OS-wide gate. Do not reuse TTS suppression as a no-action guarantee. |
| Correlation, cancellation and readiness | Producer `_safe_run_id` hashes supplied IDs; `_BridgeState.reserve_run` reserves them before inference. `InternBridgeHandler.do_GET` returns static readiness, and `do_POST` reports completion only for `bridge_request`. There is no cancel or result lookup route. `Client.Do` already verifies the hash and never retries; `ProbeGeneration` tests a fixed public request. | OS-side correlation is implementable: retain original IDs, emit one local terminal outcome, clear busy/markers on failure or shutdown, and bound pending work. Report timeout as an uncertain remote outcome, not remote cancellation or successful handoff. A real generation result can establish recent generation availability, not session state, process uptime, employee completion, or provider authenticity. These are implementable lifecycle requirements, not evidence that HTTP cannot be adapted. |
| Activation ownership | `system/device/runtime.go:updateAgentRuntime` calls `ensureSwitchRuntime`, materializers, and `runSwitchRuntime` before saving config; `RestartForAgentRuntime` restarts os-server. `runtime_installers.go:materializeInstaller` returns nil for absent installers, allowing `switch_runtime.sh:install_new` to fetch and execute the CDN installer. The switcher also controls old/new systemd units. HTTP requests readiness; MQTT retains unit-active semantics. | An externally supervised bridge needs a dedicated, verified selection path that bypasses installer/presync/CDN/unit control, handles switching both into and out of Intern, and defines config failure/restart ownership. Merely omitting an installer is not safe. Do not add a fake unit or use `remote`, which instantiates Hermes. |

The producer's `create_server` default is `http://127.0.0.1:8765`; this remains
an inspected producer default, not an activated OS endpoint. Any eventual
registration must fix that loopback endpoint under the explicit runtime name
`intern`, keep Cassi as first receptionist and Gus wake-word processing on the
producer, and never delegate to Hermes/OpenClaw. No credentials or devices were
accessed to make this assessment.

Missing sessions, image inference, skills, model configuration and channels are
not themselves reasons to require a full-brain implementation: unsupported
operations can return explicit errors, while truthful empty session/unknown
uptime values and context-bound inactive watchers can satisfy their documented
contracts. No nil interface embedding, panic, invented history, or no-op
success is acceptable. The installer/persona checklist in the historical
full-brain section below is **not** an instruction to install a Welcome Desk
service or copy unused workspace files.

The scoped fallback is complete when this audit and its Vietnamese counterpart
are recorded with local checks; runtime registration remains blocked on trusted
admission plus the tested integration gates above. See the
[current proof and limits](../receipts/intern-welcome-desk-audit-2026-09-14.md).

## Welcome Desk reassessment — 2026-09-14

Rechecked at `0f180391b22446d7fd2e63095b777bd0d34bc809` against the current
`0.2.0` / `cassi-first.v1` client. **Blocked: `intern` is not selectable,
including after this documentation change merges.** This narrower request is
for a text-only, Cassi-first Welcome Desk with no execution, credentials, or
device writes; it does not require adding employee tools, memory, or images.
The historical full-brain checklist below is not a requirement to add those
features to the Welcome Desk.

Three current integration gaps still prevent a small, safe adapter:

1. **Admission:** `AgentGateway.SendChatMessage*` and `QueuePendingEvent` carry
   no trusted data class. The sensing caller sends raw strings and replays
   them (`system/server/sensing/delivery/http/handler.go`). `Client.Do` rejects
   unknown/restricted/secret content before networking. Defaulting all chat
   to public/business defeats that boundary; rejecting every chat would not
   provide a usable backend. A trusted input policy must survive queue/replay.
2. **Lifecycle and effects:** `system/server/server.go` starts gateway event
   and watcher loops. `system/server/config_watch.go` also invokes migrations,
   MCP/user reconciliation, and conditional HAL restart/voice setup outside
   the adapter. Returning unsupported from adapter methods alone cannot
   gate those external effects. `handler_event_agent.go` consumes lifecycle
   events through the device/TTS/delivery pipeline. Bridge output already
   rejects hardware markers, but `executes_actions:false` is not an OS-wide
   capability gate. A safe text-only lifecycle needs correlated terminal
   events, bounded cancellation/queue handling, and tested suppression of
   device and channel effects. HTTP completion remains `bridge_request` only.
3. **Selection and ownership:** `system/domain/device.go:AgentRuntimes`
   excludes `intern`; the settings switch rejects it. Hand-writing
   `agent_runtime: "intern"` instead reaches the **OpenClaw fallback** in
   `system/agent/factory.go:resolveRuntime`. Do not use that as an activation
   recipe. Merely adding it to the list routes switching through
   `system/device/runtime.go` and `runtime_installers.go`: script writes,
   installation/service control, then config persistence. An externally owned
   loopback bridge needs an explicit, verified activation path with those
   effects gated. The existing `remote` option selects Hermes and is unsuitable.

No adapter, factory case, config field, installer, or default was added.
`internbridge.New(port)` remains a library constructor: nonzero port, fixed
`127.0.0.1`, no URL/proxy/redirect/credential override. The documented example
port `8765` is not a registered runtime default. Future registration must use
the explicit name `intern`, an owned fixed loopback configuration, and no
Hermes/OpenClaw delegation. Unsupported optional features can return explicit
errors; nil interface embedding, panicking methods, silent image loss, and
fabricated session/readiness success cannot complete the adapter.

Next implementation boundary: establish trusted admission and a text-only
capability/activation contract, then verify selection, real request/result
correlation, offline/protocol/custody failures, and zero device/channel effects
before registering the complete interface implementation. This audit stops
at the requested larger-adapter boundary; no device access is needed to review it.
See [local verification](../receipts/intern-welcome-desk-audit-2026-09-14.md).

## Earlier full-brain assessment

Inspected 2026-09-14: autonomous-os branch `codex/gus-runtime-contract-20260914`,
base `8254e8a14`, [PR #406](https://github.com/autonomous-ai/autonomous-os/pull/406),
the complete `system/domain/agent.go` interface and
[official runtime guide](adding-agent-runtime.md). The bridge source inspected
is project-spider-man commit `4f3e3c3606420db7bd4e72c8de3d68d5486f56c5`,
`packages/agents/src/gus/agents/{intern,intern_bridge}.py`.

Compatibility update: the client now pins PR #148 commit
`753b661087ee2ae1e4718babc2da618740cb52fa`, version `0.2.0`, schema
`cassi-first.v1`; see [the current client contract](intern-bridge-client.md).
The assessment below records the earlier registration audit. The new reception
metadata does not add employee execution, sessions, device access, or gateway
registration. The optional upstream provider also means no inference-locality
claim follows from the client probe.

## Decision

The transport can be reused, but the existing bridge cannot honestly be
registered as a complete Intern brain. `InternHarness.__call__` explicitly
implements one-shot text routing/classification/drafting, without employee
memory, tools or actions. `REQUEST_FIELDS` admits only text, operation, data
class and run ID. Adding a factory case would expose unsupported behavior to
real OS callers. This is a protocol/implementation prerequisite, not a request
for device access or permission to deploy.

This slice implements two reusable prerequisites in `system/lib/internbridge`:
pre-network rejection for unknown/restricted/secret data (all operations), and
`Client.ProbeGeneration`, a fixed public generation canary with a shared
deadline and explicit failure semantics. Neither claims runtime registration.

## Contract disposition and exact integration points

All methods of `domain.AgentGateway` were reviewed. The following groups cover
the interface; possible implementations are distinguished from current code.

| Methods / surface | Honest disposition and missing work |
|---|---|
| `SendChatMessage`, `SendSystemChatMessage`, `SendChatMessageWithRun`, `SendSlashCommandWithRun` | Transport is reusable, but these signatures carry no trusted custody metadata. `system/server/sensing/delivery/http/handler.go` dispatches plain strings and queues images. Establish trusted per-input policy and preserve it through queue/replay; never infer class from prompt, channel name, or model. Unknown must fail closed. Slash commands need explicit semantics, not ordinary draft success. |
| `SendChatMessageWithImages`, `SendChatMessageWithImagesAndRun`, `SendSlashCommandWithImagesAndRun` | Blocked: `intern_bridge.py:_validate_request` has no attachments, and `intern.py:LocalModel.complete` only sends text. Extend and test the producer protocol/model path with attachment custody; never silently discard images. Returning unsupported is honest but does not fulfill the guide's core-image requirement. |
| `StartWS`, `NextChatRunID`, `IsBusy`, `SetBusy`, `QueuePendingEvent`, `DrainPendingEvents` | Implementable OS-side in a future `runtimes/intern` service: asynchronous dispatch, correlated start/output/end/failure events, cancellation handling and bounded queue. HTTP completion is not an OS lifecycle event. A timeout leaves remote execution uncertain; do not retry automatically or claim remote cancellation. |
| `MarkGuardRun`/`ConsumeGuardRun`, `MarkBroadcastRun`/`ConsumeBroadcastRun`, `MarkPoseBucketRun`/`ConsumePoseBucketRun`, `MarkWebChatRun`/`IsWebChatRun`/`ConsumeWebChatRun`, `MarkSilentRun`/`IsSilentRun`/`ConsumeSilentRun` | Implementable synchronized OS-side state, consumed at real lifecycle boundaries. No empty marker methods. |
| `SetPendingChatTrace`, `RemovePendingChatTraceByRunID`, `MatchPendingByMessage`, `IsRecentOutboundChat` | Implementable bounded correlation/echo state. Bridge hashes supplied IDs; maintain the original OS trace mapping. |
| `Name`, `Version`, `IsReady`, `ConnectedAt`, `AgentUptime` | Name/version can be real; `/ready` in `InternBridgeHandler.do_GET` is static. New generation probe improves evidence but does not implement gateway readiness. Future service must track actual connection/generation transitions; uptime remains 0 unless known, as allowed by the interface. |
| `GetSessionKey`, `SetSessionKey`, `NewSession`, `FetchChatHistory`, `ShouldRotateSession`, `CompactSession` | No session/history/context in the producer. A local key alone does not create a model conversation. Define and implement owned session storage, consumption, rotation and reset, or explicitly narrow the product capability. Unsupported compaction/history can return `domain.ErrNotSupportedByRuntime`; fabricated history/reset success is invalid. |
| `SetupAgent`, `EnsureOnboarding`, `ResetAgent`, `RestartAgent`, `GetConfigJSON` | Need an owned installation and actual runtime configuration. The producer CLI only accepts host/port and creates a default harness. No device-writable workspace/config contract exists. Do not reset arbitrary directories or report a restart that never occurred. |
| `WatchIdentity`, `UpdateIdentityName`, `StartSkillWatcher`, all eight skill methods (`SaveSkill`, `InstallSkillArchive`, `InstallSkillMarkdown`, `ListSkills`, `ReadSkillFiles`, `ExportSkillArchive`, `ReadSkillFile`, `DeleteSkill`) | Producer consumes no persona or skill files. Define real consumed slots first, then use shared `system/skills` helpers and capability gating. Merely copying files would be dead state. Watcher signatures are implementable; void return types are not the fundamental blocker. |
| `StartModelSync`, `UpdatePrimaryModel`, `StartPrimaryModelWatch`, `RefreshModelsConfig` | Pinned `LocalModel` uses literal IPv4 loopback port 1234 and default model `qwen/qwen3-14b`, with proxies/redirects disabled. Bridge CLI has no model config input and wire results omit provider/usage. Need explicit local-only config consumption and model availability proof; no cloud fallback. In-place updates may return `ErrNotSupportedByRuntime` only if onboarding/presync really applies the config. A generation probe cannot attest an arbitrary listener's provider. |
| `WriteMCPEntry`, `RemoveMCPEntry` | No tools/MCP consumer. Return `ErrNotSupportedByRuntime` if deliberately excluded; never persist unused entries and report success. |
| `SupportedChannels`, `AddChannel`, `RefreshChannelConfig`, `HasWhatsappSession`, `PairWhatsapp`, `GetTelegramBotToken`, `GetTelegramTargets`, `Broadcast`, `SendToUser`, `SendToUserWithMedia`, `GetConfiguredChannel` | A text-only slice may declare no channels; unsupported mutation/delivery must return channel errors, pairing must emit failure. Empty token/targets/session capability are truthful only with that declaration. No credentials need be imported. |
| `SendToHALTTS`, `Speak`, `SendToHALTTSQueue`, `StopTTS`, `SetVolume`, `StartHALVoice` | Implementable through existing HAL helpers, retaining their accepted-vs-played semantics. No hardware paths were invoked in this work. |

## Registration and activation gate

After those contracts exist, add the runtime constant/list in
`system/domain/device.go`, selection and transport in `system/agent/factory.go`,
and a complete `runtimes/intern.Service` with a compile-time interface assertion.

Ship `runtimes/intern/install.go` + embedded `install.sh` with a pinned,
reproducible package source, service unit and cheap executable verification.
Embed `presync.sh` for every-switch config/skill restoration; register via
`system/lib/runtimereg.Register` and `RegisterPresync`. The existing
`system/device/runtime_installers.go` and generic `switch_runtime.sh` already
materialize/run hooks. No new switcher is needed. `RegisterReadiness` exists,
but readiness hooks are only invoked when requested; a future activation path
must require readiness, not accept systemd active alone. Account for the
switcher's retry loop before using a probe that consumes inference.

For migration, implement `system/agent/migrate_persona/runtime_intern.go` and
register it in `migrator.go` only once the backend actually consumes the mapped
persona/memory slots. Include `read`, `write`, `personaPaths`, `userProfilePath`,
round-trip proof and reset/re-presync proof. A guessed directory would allow
silent data loss and a false migration success. Hook behavior also needs a
real OS turn integration with shared capability gating, not copied dead files.

## Verification boundary

Fake loopback servers prove admission (zero requests for held data, positive
controls for admitted data), generation-vs-metadata behavior, bounded failures,
safe errors and no retries/cached success. They do not prove installed model
availability, provider authenticity, live runtime lifecycle or device readiness.
See [verification receipt](../receipts/intern-runtime-contract-2026-09-14.md).
