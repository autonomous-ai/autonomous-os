# Intern bridge transport client

`system/lib/internbridge` is an **unregistered** Go HTTP client for the loopback
Intern bridge in [project-spider-man PR #145](https://github.com/Garman-Unified-Systems/project-spider-man/pull/145),
pinned to commit `4f3e3c3606420db7bd4e72c8de3d68d5486f56c5`, protocol `0.1.0`.
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

Every request requires an explicit operation (`route`, `classify`, `generate`)
and trusted data class (`unknown`, `public`, `business`, `restricted`, `secret`).
Empty enum values are rejected locally. The data class is transmitted unchanged;
this library cannot prove that a caller classified correctly. Restricted/secret
requests are transmitted to the loopback bridge for its custody decision, not
redacted locally. Callers must enforce their custody boundary before calling.

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
  JSON rejects unknown/duplicate fields, trailing documents, nested values,
  wrong scalar types, missing required fields, invalid UTF-8, and unpaired
  UTF-16 surrogate escapes. Valid escaped emoji are preserved.
- Success requires `executes_actions:false`, `transport_status:accepted`, and
  `lifecycle_status:completed`, a recognized destination/kind, a correlated run
  ID, and an operation-appropriate status. Classification labels are restricted
  to the four protocol labels; drafts containing reasoning/hardware markers
  are rejected.
- There are no application retries. A timeout does not prove the bridge stopped
  work. Supplied IDs are reserved before inference; replay returns 409, and the
  bridge provides no result lookup or remote cancellation API.

## Results and errors

`routed`, `classified`, and `draft` return a `Result`. A routed result only
resolves a destination; it never means an action was dispatched. Draft output
is untrusted text, not commands, persona memory, or permission to perform work.

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
| Oversized response | `ErrResponseTooLarge` |
| Deadline, cancellation, other transport failure | `ErrDeadline`, `ErrCanceled`, `ErrTransport` |
| Invalid local input | `ErrInvalidRequest` |

`Health`, `Ready`, and `BridgeVersion` validate `/health`, `/ready`, and
`/version`. **Ready proves transport metadata only.** The upstream bridge
returns readiness without probing inference; this client does not invent model
availability or uptime.

## Deferred integration

Full gateway integration still needs trusted custody metadata in OS chat calls,
an error-capable contract for unsupported watcher methods, and a defined
activation/readiness contract. No install/presync/migration adapters or fake
success methods are added. Existing runtime selection and fallback behavior
are untouched.

Tests use local fake HTTP servers without models, credentials, or devices.
See the [verification receipt](../receipts/intern-bridge-client-2026-09-14.md).
