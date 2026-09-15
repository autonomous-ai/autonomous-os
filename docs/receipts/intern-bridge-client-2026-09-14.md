# Intern bridge client verification — 2026-09-14

## Scope and source

Implemented the unregistered transport-only slice in
`system/lib/internbridge/{client.go,client_test.go}`. English and Vietnamese
documentation lives in `docs/agentic/intern-bridge-client.md` and
`docs/vi/agentic/intern-bridge-client_vi.md`.

- OS base: `a293bb125bcec21bb540e3c1e12bc160a258face`.
- Source versions: OS `0.1.112`, HAL `0.1.127` (unchanged).
- Transport source: project-spider-man PR #145,
  `4f3e3c3606420db7bd4e72c8de3d68d5486f56c5`,
  `packages/agents/src/gus/agents/intern_bridge.py` and `intern.py`.
- Branch: `codex/gus-runtime-contract-20260914`.

## Runtime Proof

Local fake-server verification only; no live inference or device execution.
Toolchain: `go version go1.26.4 darwin/arm64`; module declares Go 1.24.0.

| Check | Observed result |
|---|---|
| `gofmt -w system/lib/internbridge/client.go system/lib/internbridge/client_test.go` | Completed; subsequent `gofmt -l` printed nothing |
| `go test -race -timeout 30s ./system/lib/internbridge` | PASS, exit 0 |
| `go test -cover ./system/lib/internbridge` | PASS, 91.0% statement coverage |
| `go test ./...` | PASS, exit 0; unchanged packages used Go's test cache |
| `go vet ./...` | No diagnostics |
| `git diff --check` | No whitespace errors |

Tests exercise the four HTTP endpoints, trusted classification transmission,
opaque ID hashing/correlation, every application outcome, HTTP failure
envelopes, redacted typed errors, malformed/duplicate/nested/trailing JSON,
version/type/status checks, Unicode escape validation, request/response size
boundaries, cancellation and header/body deadlines, and proxy/redirect refusal.
The race detector covered the focused package. The transport timeout is also
asserted; short caller deadlines exercise timeout behavior without a 20-second
sleep. No deployment or model-readiness claim follows from these tests.

## Boundaries

No changes to runtime registration, AgentGateway, device switching, installers,
presync, migration, launchd, firmware, credentials, or governance. No production
caller imports this library. No new module dependencies. No automatic retry,
remote cancellation, stateful session, or action execution is claimed.

The bridge's unauthenticated loopback listener is not a caller-authentication
boundary. The caller must supply trusted classification and enforce custody
before invoking the client; `/ready` proves transport metadata only. Full
gateway integration remains deferred as described in the client documentation.
