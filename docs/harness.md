# Harness integration

`system/harness` connects one Autonomous device directly to one explicitly paired Harness computer. Harness Desktop/CLI discovers devices through the existing `_autonomous._tcp` mDNS service, also used by Autonomous Buddy. Harness uses its own identity pins and the original Harness `E2eeManager` pairing/session protocol. Buddy's implementation and keys remain independent.

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

The existing mDNS advertisement points to port 80. The nginx templates in `scripts/provision/setup.sh`, `scripts/imager/build.sh` and `scripts/imager/build-orangepi.sh` include an exact `/api/harness/ws` location forwarding HTTP/1.1 Upgrade and long connection timeouts. Existing installations must receive that nginx configuration when this feature is deployed; uploading only the Go binary does not update nginx. Repository validation does not deploy or restart any device.

`system/harness/testdata/original-e2ee-protocol.json` is generated from Harness's original E2EE core. Tests cover CPace, original session signatures/keys, encrypted records, rekey, replay rejection and pairing lifecycle. `system/server/harness_test.go` checks that generating a code needs no machine selection, code reads require owner authentication, and remote/proxied agent commands are refused. Local cross-repository interoperability checks exercise the real CLI manager and Go service; they do not replace testing voice and LAN behavior on the physical device.

Run the optional cross-repository check from the OS repo after installing the CLI checkout's dependencies:

```sh
go run ./system/harness/testdata/direct_interop.go /absolute/path/to/autonomous-harness/cli
```

It temporarily advertises a local mDNS device, uses a local OS WebSocket server and the real CLI backend adapter without connecting its backend, and checks mismatch/retry, encrypted operations, deduplication, restart/reconnect and revocation. It requires local multicast networking and does not contact a physical device.
