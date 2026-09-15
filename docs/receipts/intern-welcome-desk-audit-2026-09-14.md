# Intern Welcome Desk registration audit — 2026-09-14

Scope: PR #406, existing branch `codex/gus-runtime-contract-20260914`.
Inspected OS code commit: `ed3e8b12d3b7890047f9d60bd075f1af89105d35`.
Inspected producer code commit: `16bb0d3f17083ffc9c9b466ff7470e2e4807b083`.
Outcome: documentation-only blocker; no selectable `intern` backend added.

The [English audit](../agentic/intern-runtime-contract.md) and
[Vietnamese audit](../vi/agentic/intern-runtime-contract_vi.md) distinguish
the requested no-execution Welcome Desk from the historical full-brain scope.
The blockers are trusted admission, effects outside the adapter, and activation
ownership. No nil/panicking gateway or dependency on another runtime was added.

## Full-registration follow-up

The new EN/VI section records exact callers and closure criteria:

- Admission must occur before `PostEvent` logs text, saves attachments, builds
  identity/context, or queues work. `sameOriginOrLAN` admits network/browser
  origins, not trusted classification assertions. The producer trusts the
  supplied class; it cannot repair the missing authority. Propagating an
  arbitrary JSON class or defaulting all text to business is not a solution.
- Correlation and a bounded local terminal lifecycle are implementable in Go.
  Bridge hash validation already exists. No result lookup/cancel protocol means
  timeout must remain an uncertain remote outcome, without retries or fabricated
  completion. Session/history/skills are optional for this narrower product.
- TTS suppression does not gate the shared handler's hardware call path;
  startup/config side effects also occur outside gateway methods. A text-only
  runtime needs tested gates at those callers, not just unsupported methods.
- Missing installer registration permits CDN fallback, rather than exempting
  an external bridge from installation. Selection both into and out of Intern
  needs explicit ownership and rollback semantics. The earlier full-brain
  installer checklist is historical, not the Welcome Desk implementation plan.

The producer source default is `http://127.0.0.1:8765`; no listener was contacted.
Cassi-first reception and producer-side Gus wake-word handling remain intact.
No runtime source, selection list, configuration, installer, credentials,
firmware, or device state was changed. The decisive unresolved prerequisite is
an authorized source of per-input admission, including any appended context;
the lifecycle and activation work must then prove the stated no-effect gates.

## Runtime Proof

Local repository checks, without starting os-server, a model, or a device:

| Command | Observed result |
|---|---|
| `gofmt -l system/domain/agent.go system/agent/factory.go system/device/runtime.go system/device/runtime_installers.go system/server/server.go system/server/config_watch.go system/server/middleware.go system/server/sensing/delivery/http/handler.go system/server/agent/delivery/http/handler_event_agent.go runtimes/picoclaw/events.go system/lib/internbridge` | Exit 0, no formatting differences; documentation-only change, no Go edits to format. |
| `go test -count=1 ./system/lib/internbridge ./system/agent ./system/device ./system/server/agent/delivery/http ./system/server/sensing/delivery/http` | Exit 0; all five packages pass fresh, including ingress and event consumers. |
| `go test -race -count=1 ./system/lib/internbridge` | Exit 0; bridge tests pass with the race detector. |
| `go build ./...` | Exit 0. |
| `go vet ./...` | Exit 0, no diagnostics. |
| `go test ./...` | Exit 0, all root-module packages pass (cached where applicable). |
| `git diff --check` | Exit 0, no whitespace errors. |

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
