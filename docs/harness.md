# Harness integration

`system/harness` connects one Autonomous device directly to one explicitly paired Harness computer. Harness Desktop/CLI discovers devices through the existing `_autonomous._tcp` mDNS service, also used by Autonomous Buddy. Harness uses its own identity pins and the original Harness `E2eeManager` pairing/session protocol. Buddy's implementation and keys remain independent.

## Product context and team ownership

The Harness app and its device integration are developed independently by the Harness team. This repository supplies the Autonomous OS side of that integration and the `harness-use` skill; it does not own Harness's desktop product, agent runtime or pairing protocol. The purpose is to let the device delegate coding/research tasks to agents already managed by Harness on the user's computer.

| Owner | Repository / code | Responsibility |
|-------|-------------------|----------------|
| Autonomous OS team | This repo: `skills/harness-use`, `system/harness`, `system/server/harness.go`, `system/web/src/pages/monitor/HarnessCard.tsx` | Voice/skill routing, conversation target and unresolved-delivery state, device-generated code, device-side trust/session, local API, OS Monitor and device event delivery. |
| Harness team | [autonomous-harness](https://github.com/autonomous-ai/autonomous-harness): `cli/src/lib/autonomous-device`, `cli/src/lib/e2ee`, `cli/src/backendSocket.ts` | Computer-side discovery/reconnect, original pairing/E2EE, agent operations, capability negotiation, receipts/events and the CLI management API. |
| Harness team | [autonomous-harness-desktop](https://github.com/autonomous-ai/autonomous-harness-desktop): `lib/autonomous_device`, `lib/settings/sections/devices_section.dart` | Desktop pairing/management UI over its local Harness CLI. The Desktop UI does not own device trust or execute the skill. |

The execution path is: user/voice → `harness-use` → OS loopback API → authenticated direct connection → Harness CLI → selected computer agent. For an explicit request to a named agent, OS adds internal routing context that selects `harness-use` and excludes Buddy skills, including in a model session that still has older skill instructions. An explicit request for Autonomous Buddy overrides this Harness route and remains with the Buddy skill. The named Harness agent is the execution target: OS directs the skill to send its underlying task, never a request to contact or ask that same agent. A live follow-up window is only a hint; vague, unrelated, or uncertain voice input stays with the main agent unless it clearly continues the Harness task or answers its open question. A `send` or `answer` receipt in `queued`, `delivered`, `started`, `completed`, or `rejected` is a known outcome and ends the skill work immediately: the model makes no more Harness or shell calls, including `receipt`, `status`, `recap`, `list`, or a second mutation, and returns `NO_REPLY`. It may inspect a receipt only after `DeliveryUnknown`/no usable receipt or at the user's explicit request, and must never automatically resend. OS receives lifecycle events and delivers the final result directly. For a user turn, the skill records the local response target when it sends and returns no device-agent prose. Real Harness lifecycle events show acceptance and work in the pending Web Chat response. On terminal `turn.summary`, OS reads the latest `recap` RPC entry and chooses `turns[].fullText`, then `turn.summary.fullText`, then legacy `text` as the final response. `fullText` is the bounded complete user-facing answer; `text` remains a compact preview for older CLIs and device cards. Voice records that direct result in realtime history before any later follow-up, and the next short follow-up receives it as untrusted context for the main runtime. If the agent opens a structured question, OS delivers that question to the original turn; the next routed answer calls `status`, answers the live question with its exact request ID and answer keys, and routes the eventual result to that follow-up turn. Voice speaks direct Harness content, while Web Chat displays it without TTS. Callback frames are not injected as JSON sensing events, so they cannot create a second turn or an altered device-agent answer. `harness-use` takes precedence when the user asks an agent to work, including browser research; `computer-use` is for direct visible Mac UI work and Buddy remains available only when explicitly requested.

Autonomous Buddy is retained separately. This feature does not invoke Buddy, share its pairing keys or require its connection. Reusing the device's existing mDNS advertisement does not combine the two trust stores. Keep the integration device-neutral: `autonomous-device` is the CLI namespace, not `lamp`.

## Contract references and coordination

The Harness team's CLI contract is documented in `docs/autonomous-device-integration.md` and `docs/vi/autonomous-device-integration_vi.md` in its repository. Verify it against the matching CLI implementation, especially `command.ts`, `localApi.ts`, the direct connection/application handlers and the original `e2ee` core/manager. This OS document describes the implementation in this repository; it does not freeze or override the other team's evolving API. The protocol vectors and cross-repository test below are compatibility evidence, not an independent specification to invent behavior from.

The initial integration is tracked by [OS PR #316](https://github.com/autonomous-ai/autonomous-os/pull/316), [CLI PR #24](https://github.com/autonomous-ai/autonomous-harness/pull/24) and [Desktop PR #8](https://github.com/autonomous-ai/autonomous-harness-desktop/pull/8). These identify the collaborating changes, not a claim that any particular release has deployed them. Check the actual CLI revision used for each interoperability run; PR descriptions and earlier implementation experiments can be stale.

For future OS work:

1. Keep edits in this OS repository. The Harness team's agents maintain the other repositories; send concrete contract discrepancies for coordination instead of silently changing their implementation.
2. Before changing commands, frame shapes, pairing direction, receipt meaning or capabilities, compare the current owner contract and code. Record the relevant Harness revision and agree the compatible change with that team. A missing API or mismatched version is not a reason to invent a new flow, backend credential or transport.
3. Preserve the agreed flow: device generates the code, computer discovers the device and accepts the code, then connects directly. Pairing/session cryptography follows Harness; OS does not define another scheme. Missing capabilities must be reported, never bypassed with terminal input or Buddy.
4. Update both OS language documents and applicable fixtures when the agreed contract changes, then run the cross-repository check against that CLI revision. Local test success does not establish compatibility with a different installed CLI build.
5. Repository work and local verification are separate from device rollout. The owner performs physical-device/manual voice testing; deployment, SSH and restarts require explicit authorization. Include the nginx update described below in deployment handoff.

## Pairing

1. In the device's OS Monitor, choose **Generate pairing code**. The device creates a cryptographically random six-character code valid for 60 seconds.
2. On the same local network, open Harness Desktop → Settings → Devices, select the discovered Autonomous device, and enter its code.
3. CLI users can run `harness autonomous-device discover --json`, then `harness autonomous-device pair --device <discoveryId> --code-stdin`, supplying the code on stdin.
4. Harness opens a WebSocket to the discovered device's `/api/harness/ws`. After pairing, the device can list and interact with that computer's agents through `harness-use`.

Discovery resolves the advertised host and port; it does not authenticate a device. No IP field or backend credentials are needed on the device. Harness's existing Mac login/start requirements remain unchanged; the device connection itself does not traverse the backend and works independently of its live connection. When no device appears, check that both are on the same network and mDNS is permitted.

The socket exchanges `machine_select` / `machine_selected` for the CLI's machine identity, then the OS sends the original `e2e_pair_intent` with `{pairId,label,role:"device"}`. The code is never transmitted on this socket or written to the trust store. Original CPace rounds use `autonomous-e2e-pair|agent:<machineId>|a:adapter|b:device`. Original signed `e2e_hello` / encrypted `e2e_welcome` establish the session. This reuses Harness's cryptographic protocol over a direct connection; the old cloud transport is not involved.

An incorrect code fails the attempt and is shown in Desktop. Generate a new code on the device before retrying. Completion, failure, cancellation and expiry clear the code. Pairing refuses to overwrite the existing computer; unpair first to choose another. CLI/Desktop may retain separately paired devices, each with its own identity.

## OS API and lifecycle

- `NewService(configDir, callbacks)` loads the identity; `Start(ctx)` owns the lifetime.
- Admin-authenticated `POST /api/harness/pair` calls `StartPair(ctx)` without a machine ID. `GET /api/harness/pair/status` returns the temporary code, `expires_at`, `pairing`, `state` and optional error with `Cache-Control: no-store`.
- Admin-authenticated `POST /api/harness/pair/cancel` invalidates the pending attempt. `DELETE /api/harness` removes the pin and closes its sockets.
- `GET /api/harness/status` is available to the administrator or strict loopback callers; it never returns the code.
- `GET /api/harness/ws` accepts direct CLI sockets. PAKE and pinned E2EE authenticate this route, rather than an HTTP bearer. Browser Origin headers are refused. At most four incoming sockets are allowed, with a ten-second initial metadata timeout and a twenty-second session/application handshake timeout.
- `POST /api/harness/request` is strict loopback only, including proxy-address checks, for the skill runtime.

If pairing is interrupted after an authenticated provisional pin is saved, OS Monitor shows the retained computer and offers Unpair instead of generating a conflicting code. If removing trust fails to persist, the API returns an error and retains the previous pin in memory so status agrees with disk; retry Unpair after correcting the storage error.

CLI owns discovery and reconnect. OS waits for incoming authenticated sockets; the saved pin survives process restart. A new connection replaces an existing connection only after authentication and application negotiation succeed. Active sockets send WebSocket ping every 15 seconds and require a pong within the 60-second read deadline.

The OS identity and one computer pin are stored in `configDir/harness/trust.json` (directory 0700, file 0600). Writes are atomic and symlinks are refused. A malformed trust file fails initialization instead of silently generating a replacement identity. Round-three authenticated pins are provisional for five minutes so a lost final PAKE message can recover through original `e2e_hello`; successful session establishment confirms the pin. The direct protocol marker is `harness-device-direct-v1`; compatible original-E2EE relay pins are accepted, but the earlier experimental custom-crypto pins are not.

## Encrypted agent operations

After authentication, encrypted `autonomous_device_request` carries application `hello` to negotiate capabilities and event resume. Replies use `autonomous_device_result`; events use `autonomous_device_event`. Original pairwise/group keys, signature domains, key derivation, authenticated rekey and replay rejection remain in use. Plaintext application results are refused.

Supported operations are `agents.list`, `turn.send`, `turn.stop`, `status`, `recap`, `question.answer` and `receipt.get`. Agent-addressed operations require explicit machine and agent IDs. Tool permission approval, raw terminal input, arbitrary shell/file access and agent creation/deletion are outside this integration.

Mutations require a stable idempotency key. OS sends once and waits at most 30 seconds. A timeout/disconnect after sending returns `DeliveryUnknownError`: query the receipt with the same key rather than automatically resend. There are at most 64 pending requests and 128 queued callback events. Event resume uses `serverInstanceId` and numeric `eventId`; resync requires refreshing agent state. The skill retains an explicit agent per conversation and unresolved mutations across invocations.

## Nginx and verification

The existing mDNS advertisement points to port 80. The nginx templates in `scripts/provision/setup.sh`, `scripts/imager/build.sh` and `scripts/imager/build-orangepi.sh` include an exact `/api/harness/ws` location forwarding HTTP/1.1 Upgrade and long connection timeouts. Existing installations must receive that nginx configuration when this feature is deployed; uploading only the Go binary does not update nginx. The canonical `software-update` updater must apply this idempotent nginx migration before an OS/web Harness rollout, because older images do not acquire provisioner templates through component OTA. Repository validation does not deploy or restart any device.

`system/harness/testdata/original-e2ee-protocol.json` is generated from Harness's original E2EE core. Tests cover CPace, original session signatures/keys, encrypted records, rekey, replay rejection and pairing lifecycle. `system/server/harness_test.go` checks that generating a code needs no machine selection, code reads require owner authentication, and remote/proxied agent commands are refused. Local cross-repository interoperability checks exercise the real CLI manager and Go service; they do not replace testing voice and LAN behavior on the physical device.

Run the optional cross-repository check from the OS repo after installing the CLI checkout's dependencies:

```sh
go run ./system/harness/testdata/direct_interop.go /absolute/path/to/autonomous-harness/cli
```

It temporarily advertises a local mDNS device, uses a local OS WebSocket server and the real CLI backend adapter without connecting its backend, and checks mismatch/retry, encrypted operations, deduplication, restart/reconnect and revocation. It requires local multicast networking and does not contact a physical device.

### MQTT pairing control

Authenticated device MQTT data commands expose `harness.pair.start`, `harness.status`, `harness.pair.cancel`, and `harness.pair.revoke`. Replies are published on the device fd channel. The direct WebSocket remains the data channel for paired Harness computers.

Codex queue replay restores the original `harness-reply` address for Web/MQTT chat and voice follow-ups. A queued request uses the same local run ID and channel as an immediately dispatched request.
