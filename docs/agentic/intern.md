# Intern Welcome Desk runtime

## Voice admission — 2026-09-15

The custom `intern` runtime cannot currently serve the device microphone or
speak its replies. The `intern-v2` hardware declaration does not enable those
paths in this runtime. Do not treat successful administrator chat, bridge
generation, or the readiness flag as voice readiness.

Microphone-derived text has a separate, default-disabled admission endpoint.
The existing signed administrator session is the authority. An administrator
must grant that session and classify each exact transcript; no automatic
speaker authentication or semantic privacy classifier is claimed. “Gus” and
other wake/address metadata never grant trust or supply classification.

All voice requests require `Authorization: Bearer <existing signed admin session>`
and the existing direct administrator admission checks (no Origin or query).
The legacy provider-key bearer and ambient cookies cannot grant voice access.
No login, credential provisioning or new authentication store is introduced.

1. `POST /api/agent/intern/voice/grant` with
   `{"expires_in_seconds":300,"admission":"administrator_grants_voice_session"}`.
   Expiry is mandatory, 1–300 seconds, capped by the signed session expiry.
2. `POST /api/agent/intern/voice/chat` using the same session and the exact
   `text`, `operation`, `data_class`, and `admission` fields documented below.
   Only explicitly classified public/business transcripts enter the queue.
3. `DELETE /api/agent/intern/voice/grant` disables the grant. Any valid signed
   administrator session can revoke it. Poll the existing result endpoint.

There is one in-memory grant per router lifetime. Replacing it cancels its old
work. Only the session fingerprint and cancellation/deadline state are retained;
neither a session token nor transcripts enter the grant. Other session tokens
and fresh routers inherit no permission. Existing stateless session tokens
identify a bearer, not a speaker or person; sessions issued with the same expiry
have identical tokens under the existing single-admin authentication design.
Results retain the existing administrator-wide access and bounded retention.

Expiry/revocation prevents queued voice work from reaching the bridge and
cancels an in-flight HTTP wait. Already-sent work reports an unknown remote
outcome on cancellation; this cannot undo inference already performed. Restart
starts disabled. Grants do not classify subsequent inputs implicitly, append
speaker/context data, enable sensing/HAL/TTS, or change provider selection.
Authenticated typed chat remains its existing per-body admission surface;
callers must send microphone-derived text through the voice endpoint. The server
cannot infer a transcript's physical origin from arbitrary text submitted by a
trusted administrator. No untrusted microphone producer is wired in this slice.

See the [grant verification receipt](../receipts/intern-voice-grant-2026-09-15.md).
The [earlier blocker receipt](../receipts/intern-voice-contract-2026-09-14.md)
remains historical evidence for the still-unwired device microphone/TTS path.

`intern` is an explicitly selected, externally owned, text-only runtime. It is
not a device brain and does not install, start, stop, or fall back to OpenClaw,
Hermes, or another runtime. Selecting or leaving it updates only
`agent_runtime`; an operator-managed `os-server` restart activates the saved
selection.

Before that restart, the instantiated gateway continues handling channels,
sensing, configuration updates, and startup reconciliation. These effects and
their runtime markers use the active gateway, not the saved selection. Runtime
switch requests still check the saved selection separately to enforce the
operator-managed restart boundary in both directions.

When active, `os-server` binds its restricted HTTP surface to `127.0.0.1` and
constructs no HAL, sensing, MQTT, channel, skill, firmware, or device handlers.
The process now owns a native Go bridge; no Python installation is required.
The unchanged runtime client sends admitted requests only to the fixed bridge at
`http://127.0.0.1:8765/v1/intern`. It does not load or forward credentials,
follow redirects, use proxy settings, attach images or history, or execute the
returned reception route.

See [native bridge configuration and bounds](intern-native-bridge.md) for local
Ollama defaults and explicit remote-provider configuration. Provider credentials
are used only by that bridge for an explicitly configured remote endpoint.

## Admission and results

An authenticated administrator can submit an exact text body to
`POST /api/agent/intern/chat` with:

- `operation`: `route`, `reception`, `classify`, or `generate`
- `data_class`: `public` or `business`
- `admission`: the literal `administrator_classified_exact_text`

Unknown, restricted, and secret data are rejected before a bridge request.
Duplicate, mixed-case, unknown, oversized, or invalid JSON fields are rejected.
The accepted response contains a local `run_id`; poll
`GET /api/agent/intern/result/:run_id` for the bounded result. Completion means
only that the bridge request completed. Results are drafts or reception
metadata with `executes_actions: false`; no handoff or action is performed.

Readiness is not fabricated. It becomes true only after a recent, validated
draft response and expires after 30 seconds. Bridge health alone does not claim
model readiness.

All other paths return “not supported by runtime.” Runtime selection remains
available at `GET/POST /api/device/agent-runtime`, requires administrator
authentication, performs no service changes, and reports that restart is
required.
