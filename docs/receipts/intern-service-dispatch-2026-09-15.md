# Intern authenticated service dispatch — 2026-09-15

Origin: engineering@dru using Codex. Bead: dru-so0kl.
Worktree: `/Users/dru/DEV/autonomous-os-gus-runtime-20260914`.
Branch: `codex/gus-runtime-contract-20260914` (existing branch).

## Staged scope

The Intern service owns an optional `ServiceDispatcher`. The normal loopback
client remains fail-closed on service routes; `DoForService` exposes validated
proposals only to the configured owning service. Its existing worker calls the
dispatcher once, preserving the OS run ID and hashed bridge correlation.
Only `mcavoy@lab` and `pam@gus` pass the dispatch boundary. All other destinations
are rejected there; ordinary persona routes remain local. Home-control content,
smart-home and restricted/secret data remain custody-held; unknown classification
cannot leave local reception. No firmware, Wi-Fi, device state, provider keys,
authority implementation, new queue/listener, deployment, push or merge changes.

## Contract evidence inspected before editing

- `runtimes/intern/service.go`, `runtimes/intern/bridge/provider.go`,
  `system/lib/internbridge/client.go`, `system/server/intern.go`, existing route
  and voice tests, and Intern bridge documentation in this worktree.
- `/Users/dru/.autonomous/tools/fleet_dispatch_pkg/fleet_dispatch/__init__.py`:
  `_validate_authority_url`, `_substrate_task_payload`, `_post` pin the existing
  authority and establish the payload, Bearer header and `X-GUS-Principal`.
- `/Users/dru/.autonomous/scripts/gus_authority.py`: dispatch admission,
  `_requirements`, board `select`, `_dispatch_response`, HTTP boundary. Exact
  `requirements.node` and `requirements.roles` constrain authority selection;
  Intern does not select a model or contact a worker directly.
- `/Users/dru/thoughts/shared/receipts/substrate-7370-auth-probe-26.8.17.2-2026-08-17.md`:
  historical client-to-authority Bearer authentication evidence. This receipt
  does not treat that historical probe as current runtime/authentication proof.

## Runtime configuration and custody

Absent `GUS_INTERN_DISPATCH_TOKEN` means no default dispatcher, no credential
resolution and no authority network effects. Explicit opt-in also requires
`GUS_INTERN_DISPATCH_PRINCIPAL` matching an existing authorized fleet-dispatch
principal; invalid startup configuration disables dispatch. The optional URL
must exactly equal the existing canonical `http://100.115.27.81:7370`.
Endpoint, principal and token source are injectable without reading live keys.
Tests inject an HTTP transport under the canonical URL. Token removal is checked
again before each request. No credential values are logged, returned or committed;
there is no provider-key or other-client fallback, proxy, redirect or retry.

Only admitted input is sent, never bridge output, reasoning, memory or history.
Local final-only/no-thinking validation and voice cancellation remain unchanged.
Dispatch acknowledges authority acceptance only; it cannot prove downstream
final-only inference, named-employee execution, or delivery. No peer fan-out is
introduced. Node/role board compatibility must be verified before activation.

## Response contract and deployment prerequisites

HTTP 200/202 must carry JSON with `ok=true`, matching `task_id`, and exactly
`status=accepted|queued`. Optional `delivered` must be false. Completion/delivery
statuses including `REPORTED_COMPLETE` are rejected, even with `ok=true` and
correct correlation. Responses are bounded, checked for duplicate/trailing JSON,
and discarded; only locally constructed acknowledgement scalars enter receipts.
Errors are fixed and never expose authority, credential-source or transport text.
Ambiguous failures mark remote outcome unknown and are not automatically retried.

The result retains `run_id` and `bridge_run_id`, sets `scope=service_dispatch`,
and includes a `dispatch` receipt with `task_id`, destination,
`status=accepted|queued`, `delivered=false`. This is a terminal local acknowledgement
with the existing five-minute result TTL, not execution completion or readiness.

The inspected authority implementation currently emits `REPORTED_COMPLETE` for
successful worker execution. Per the Director's tightened requirement, this seam
intentionally rejects it. Before enabling outbound behavior, the Director must
assign the correct Intern credential/principal and authorize deployment; the
existing authority must expose a compatible accepted/queued response and its
board must satisfy the requested service roles. This slice changes none of those
external systems. Tests use synthetic credentials only; no live dispatch occurred.

## Runtime Proof

All commands ran from the worktree root on 2026-09-15:

| Exact command | Observed result |
|---|---|
| `gofmt -w runtimes/intern/dispatch.go runtimes/intern/dispatch_test.go runtimes/intern/service.go system/lib/internbridge/client.go` | Exit 0; formatting applied. |
| `gofmt -l runtimes/intern/dispatch.go runtimes/intern/dispatch_test.go runtimes/intern/service.go system/lib/internbridge/client.go` | Exit 0, empty output. |
| `go test ./runtimes/intern/... ./system/lib/internbridge ./system/server` | Exit 0; all four packages passed, including existing route and voice tests. |
| `go test ./...` | Exit 0; every package passed or had no test files. |
| `go test -race ./...` | Exit 0; every package passed or had no test files; no race reports. |
| `go vet ./...` | Exit 0, no diagnostics. |
| `git diff --check` | Exit 0, no whitespace errors. |

Focused dispatch tests cover missing/removed credentials and canceled contexts
with zero transport calls, invalid URL/principal configuration, both services'
payloads and headers, original correlation, persona non-dispatch, custody holds,
malformed/oversized/duplicate JSON, HTTP errors, redirect non-forwarding,
completion-status refusal, synthetic-secret error/body exclusion, and immutable
receipt copies. The full/race suites also retain existing route and voice proof.
No live key, provider, fleet endpoint, or device was used for these new tests.
Provider billing/token usage for this coding session is unavailable; it is not
reported as zero. Test timings come from Go output, not estimated billing.
