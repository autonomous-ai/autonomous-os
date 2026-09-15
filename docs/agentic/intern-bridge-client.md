# Intern bridge transport client

`system/lib/internbridge` is an **unregistered** Go HTTP client for the loopback
Intern bridge in [project-spider-man PR #148](https://github.com/Garman-Unified-Systems/project-spider-man/pull/148),
pinned to commit `753b661087ee2ae1e4718babc2da618740cb52fa`, protocol `0.2.0`
and response schema `cassi-first.v1`. Legacy `0.1.0` envelopes are rejected,
including legacy shapes relabeled with the new version.
It is not an `AgentGateway` implementation and has no production caller,
runtime registration, installation, device switching, or hardware integration.

## Calling contract

```go
client, err := internbridge.New(8765)
if err != nil {
    return err
}
defer client.CloseIdleConnections()

// classification must be supplied by trusted caller policy, not inferred from
// text, selected by an untrusted end user, or defaulted to business/public.
result, err := client.Do(ctx, internbridge.Request{
    Text: text,
    Operation: internbridge.Generate,
    DataClass: classification,
})
```

Import `go.autonomous.ai/os/system/lib/internbridge`. The bridge must already
exist on the same host. The constructor accepts only a nonzero TCP port; the
host is always literal `127.0.0.1`. No DNS, URL override, proxy, redirect,
cookie jar, authorization header, or credential discovery is used.

Every request requires an explicit operation (`route`, `reception`, `classify`, `generate`)
and trusted data class (`unknown`, `public`, `business`, `restricted`, `secret`).
Empty enum values are rejected locally. For otherwise valid requests, unknown
data returns `ErrNeedsClassification`; restricted/secret data returns
`ErrCustodyHold`, before any network request, including `route`. These local
errors have HTTP status 0 and no result. Only public/business data may be sent,
with its classification unchanged. This library cannot prove that a caller
classified correctly; a misleading label does not grant custody permission.

Text must be nonblank valid UTF-8, at most 8,000 Unicode code points. The
encoded JSON body must also fit 16 KiB; escaping and multibyte text count toward
that byte limit. Nothing is truncated. Optional run IDs must match
`[A-Za-z0-9._-]{1,64}`. Do not put sensitive data in IDs. Supplied IDs must be
returned as `run-` plus the first 24 hexadecimal SHA-256 characters; omitted
IDs must produce the bridge's 32-hex-character generated ID.

## Bounds and validation

- Each call has a 20-second maximum, including connection acquisition, headers,
  and body reads. An earlier caller deadline/cancellation wins.
- Responses are limited to 64 KiB, headers to 8 KiB, and connections per host
  to four. Response compression and proxy discovery are disabled.
- Only HTTP 200 with the pinned version and matching envelope/status succeeds.
  JSON rejects unknown/duplicate fields, trailing documents, arbitrary nested values,
  wrong scalar types, missing required fields, invalid UTF-8, and unpaired
  UTF-16 surrogate escapes. Valid escaped emoji are preserved. Only the exact
  six-field `reception_route` object may be nested; its fields must be scalar.
- Success requires `executes_actions:false`, `transport_status:accepted`, and
  `lifecycle_status:completed`, `lifecycle_scope:bridge_request`, a recognized destination/kind, a correlated run
  ID, and an operation-appropriate status. Classification labels are restricted
  to the four protocol labels. Output is at most 4,096 Unicode code points;
  output containing `<think` or `[HW:` markers is rejected for all statuses.
- There are no application retries. A timeout does not prove the bridge stopped
  work. Supplied IDs are reserved before inference; replay returns 409, and the
  bridge provides no result lookup or remote cancellation API.

## Results and errors

`reception_route` (only for `Route`/`Reception`), `classified`, and `draft`
return a `Result`. Reception only proposes a route; it never means an action
was dispatched or an employee completed work. Draft output
is untrusted text, not commands, persona memory, or permission to perform work.

`Result.Destination` is always `cassi@mama`, the first contact.
`RequestedDestination` and `Kind` describe the proposed internal route:
`orchestration@gus`, `rex@dru`, `melvil@lab`, `cassi@mama` (persona), or
`pam@gus`, logical `mcavoy`, `smart-home` (service). `news` is an intent,
not a destination or node address. Cassi and smart-home requested routes must
be custody holds. A first-contact Cassi envelope alone is not a custody hold.

`Result.ReceptionRoute` contains typed `FirstDestination`, `Handoff`, `Intent`,
`Status`, `Executed`, and `NextStep` fields. First destination must be Cassi;
status is `reception_route`, executed is false, and next step is
`safe_escalation`. Handoff is wire null (an empty string in the public struct)
or one allowlisted requested destination, never Cassi. Unknown intent or custody
hold requires null handoff. Arrays, extra fields, duplicate keys, and nested
handoff objects are rejected. Allowed intents are `orchestration`, `engineering`,
`service`, `library`, `reception`, `smart-home`, `news`, `unknown`, and
`address_or_default`. These fields are metadata, not a device/action API.

Service destinations require `service_route` and return `ErrServiceRoute` with
nil result; the client never follows the handoff. Generation cannot succeed on
a reception-only result. A hold must omit `output` entirely (even null is
rejected) and never exposes a result. Completion describes only the HTTP bridge
request, not reception, handoff, or service execution.

All failures return a nil result and `*internbridge.Error`. Use `errors.Is`
with the package sentinels; `errors.As` exposes the numeric HTTP status when
available. Error text and its unwrap chain contain only fixed sentinels, never
request text, server error/output text, URLs, or underlying network/parser
errors. The library does not log content.

| Bridge outcome | Error sentinel |
|---|---|
| `custody_hold` (must omit output) | `ErrCustodyHold` |
| `needs_classification` | `ErrNeedsClassification` |
| `needs_input` | `ErrNeedsInput` |
| `service_route` | `ErrServiceRoute` |
| `fallback`, valid HTTP 500 envelope | `ErrUnavailable` |
| Valid HTTP 400/404/409/413 envelope | `ErrRejected` |
| Invalid JSON/envelope/version/status, redirect | `ErrProtocol` |
| Valid HTTP 502 invalid-harness envelope | `ErrProtocol` |
| Oversized response | `ErrResponseTooLarge` |
| Deadline, cancellation, other transport failure | `ErrDeadline`, `ErrCanceled`, `ErrTransport` |
| Invalid local input | `ErrInvalidRequest` |

`Health`, `Ready`, and `BridgeVersion` validate `/health`, `/ready`, and
`/version`. **Ready proves transport metadata only.** The upstream bridge
returns readiness without probing inference; this client does not invent model
availability or uptime.

`ProbeGeneration(ctx)` is a separate, explicit inference probe: it checks
`Ready`, then submits the fixed public prompt `Write a brief greeting.` using
`generate`. Only a validated draft succeeds. Fallback, routing-only responses,
malformed replies and transport failures remain errors. Both stages share one
20-second deadline (or the caller's earlier deadline), with no retry, cached
readiness, user content, or reused run ID. Repeated probes consume inference
capacity (up to 256 generated tokens each with the pinned bridge); callers must
bound their frequency. This is not an automatic background health check.

```go
if err := client.ProbeGeneration(ctx); err != nil {
    return err // Bridge metadata alone is insufficient.
}
```

Success proves one generation response at that instant, not `AgentGateway`
readiness, model identity/locality, process uptime, or authenticity of the
listener. Local model routing is enforced by the pinned bridge's `LocalModel`
implementation; protocol 0.2.0 does not expose provider attestation. The upstream
harness also supports an optional provider, so transport success cannot attest
local inference.

## Deferred integration

Full gateway integration is blocked by the [runtime contract audit](intern-runtime-contract.md).
It needs trusted custody propagation, image/session/persona/skill behavior and
owned activation/configuration. Watchers are implementable in Go; their void
signatures alone are not a reason to change the shared interface. No installer,
presync, migration adapter or selectable runtime is registered in this slice.

Tests use local fake HTTP servers without models, credentials, or devices.
See the [Cassi-first verification receipt](../receipts/intern-runtime-contract-2026-09-14.md)
and the [historical 0.1.0 receipt](../receipts/intern-bridge-client-2026-09-14.md).
