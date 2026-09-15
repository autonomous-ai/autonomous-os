# Intern Cassi-first compatibility verification — 2026-09-14

Origin: engineering@dru, Codex. Existing PR #406 and branch
`codex/gus-runtime-contract-20260914`; starting commit
`8254e8a14d88617bd6c77e108d55c070888e02f0`.

## Source and scope

Inspected the local, clean project-spider-man welcome-desk checkout and
confirmed its HEAD equals [PR #148](https://github.com/Garman-Unified-Systems/project-spider-man/pull/148)
head `753b661087ee2ae1e4718babc2da618740cb52fa`. Read
`packages/agents/src/gus/agents/intern_bridge.py` (especially `_reception_body`,
`_response_body`, GET metadata and error envelopes), `intern.py` route/status
construction, and the mocked bridge tests. No bridge or model request was made.

Client pins version `0.2.0`, schema `cassi-first.v1`, and lifecycle scope
`bridge_request`. Destination is Cassi; requested destination and typed
reception metadata describe at most one proposal. Service/news proposals never
return success or output. Cassi/smart-home holds omit output and handoff.
Legacy success and metadata envelopes are rejected. Logical news routing uses
destination `mcavoy`, intent `news`; no node address is invented.

Preserved the existing uncommitted custody admission, generation-probe code,
tests, and runtime assessment docs in this worktree, updating their fixtures
and protocol references. All implementation changes stay in internbridge.
No runtime registration, action execution, device call, credential discovery,
branch switch, live-service change, or deployment occurred.

## Runtime Proof

All HTTP test traffic uses ephemeral mocked loopback servers. New tests cover
every supported operation, persona and service proposals, news routing,
zero/one handoff, malformed/duplicate/missing/null/unknown fields, multiple
handoffs, executed flags, custody output leakage, legacy rejection, strict error
envelopes, output length, and a generation probe rejecting mere reception.
Existing correlation, transport, Unicode, deadlines, proxy/redirect refusal,
size bounds, and local custody admission tests continue to pass.

- `gofmt` applied to changed Go files; `gofmt -l system/lib/internbridge` empty.
- `go test -race -timeout 30s ./system/lib/internbridge`: PASS.
- `go test -cover ./system/lib/internbridge`: PASS, 92.4% statement coverage.
- `go build ./...`, `go vet ./...`, `go test ./...`: PASS; no build/vet diagnostics.
- `git diff --check`: PASS.

Go checks match the repository's Go CI job. Web and HAL sources are unchanged;
their checks were not run locally. These tests do not establish installed model
availability, provider identity/locality, or device readiness. The registration
audit remains an assessment, not a registered AgentGateway implementation.

## Review and handoff

English and Vietnamese client docs describe the new contract; the inherited
runtime assessments explicitly identify their earlier source snapshot.
Staged diff passed redacted gitleaks (no leaks) and additional secret-family
patterns for Notion, OpenAI, AWS, HuggingFace, GitHub, and private-key headers
(no matches). The diff was also reviewed for unintended content and scope.
No credential files or credential values are needed for verification.

Provider token/billing counters are unavailable from the task tools; no zero
usage or spending claim is made. GitHub's final commit and check state are
reported in the task handoff after push. No merge is included in this scope.
