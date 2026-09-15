# Intern Welcome Desk registration audit — 2026-09-14

Scope: PR #406, existing branch `codex/gus-runtime-contract-20260914`.
Inspected code commit: `0f180391b22446d7fd2e63095b777bd0d34bc809`.
Outcome: documentation-only blocker; no selectable `intern` backend added.

The [English audit](../agentic/intern-runtime-contract.md) and
[Vietnamese audit](../vi/agentic/intern-runtime-contract_vi.md) distinguish
the requested no-execution Welcome Desk from the historical full-brain scope.
The blockers are trusted admission, effects outside the adapter, and activation
ownership. No nil/panicking gateway or dependency on another runtime was added.

## Runtime Proof

Local repository checks, without starting os-server, a model, or a device:

| Command | Observed result |
|---|---|
| `gofmt -l system/domain/agent.go system/agent/factory.go runtimes/picoclaw/service.go system/lib/internbridge` | Exit 0, no formatting differences; no Go edits to format. |
| `go test ./system/lib/internbridge ./system/agent ./system/device ./runtimes/picoclaw` | Exit 0, all four packages pass (cached). |
| `go test -count=1 ./system/lib/internbridge ./system/agent ./system/device ./runtimes/picoclaw` | Exit 0, fresh execution of all four packages passes. |
| `go build ./...` | Exit 0. |
| `go vet ./...` | Exit 0, no diagnostics. |
| `go test ./...` | Exit 0, all root-module packages pass (cached where applicable). |

Existing bridge tests cover pre-network custody rejection with positive controls,
Cassi-first metadata, no execution claims, malformed envelopes, bounded failures,
proxy/redirect rejection and generation-vs-metadata readiness. They do not prove
gateway selection: no such adapter exists. Factory/domain source inspection
shows `intern` is absent; unknown raw config falls back to OpenClaw, while the
settings switch rejects unknown names. No runtime config was changed to test
that fallback, and no fallback service was started.

No device/firmware deployment, credentials, external bridge inference, or new
runtime installation was used. Root-module Go checks do not exercise nested
integration modules, the Python producer, hardware, or deployed lifecycle.
Provider token/billing usage was unavailable for this audit; no zero-cost claim.
Merge updates documentation only; it does not make the custom runtime selectable.
