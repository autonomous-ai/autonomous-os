# Intern authority enqueue integration — 2026-09-15

Origin: engineering@dru, Codex. Source changes and local verification only.

## Scope and lineage

- New clean worktree: `/Users/dru/DEV/autonomous-os-intern-authority-20260915`.
- Branch: `codex/intern-authority-20260915`.
- Base: `codex/gus-runtime-contract-20260914`,
  `21c9ff3dbf402a6d9da325f719f4b1753cd280ad` (PR #406).
- Dependency: [dotfiles PR #35037](https://github.com/idirectships/dotfiles/pull/35037),
  observed OPEN. Inspected local dependency branch
  `codex/intern-authority-pam-custody-2026-09-15` at
  `b9ce8aa3d4fd55c247d89f9108a43657a00c1b3d` via read-only `git show`:
  `gus_service_task.py`, `gus_authority.py`, `gus_comms_contract.py`, and
  `tests/test_intern_service_custody.py` under `.autonomous/scripts/`.
- Read existing Intern service, dispatch, bridge client, and tests before edits.
  No edits to the live `/Users/dru` checkout or other existing worktrees.

## Change

Replace legacy `/dispatch` with pinned `/messages/post-task`, authenticated as
`intern@gus`. Emit exactly the six common envelope keys and the six-field
`gus-bus-task/v1` task. Its four-field body carries only admitted request text,
the allowed destination/intent pair, and an idempotency key equal to the original
OS request/task ID. Reject invalid ID prefixes, nonprintable or untrimmed text,
and text over 2,000 UTF-8 bytes before resolving credentials or sending.
Existing public/business admission and home-control custody checks remain.
The authority retains its additional secret and normalized-content screening.

Validate all seven enqueue acknowledgement fields, including version,
destination, boolean idempotent, and authority-derived message ID. Optional
`delivered` is accepted only as false. Reject extra fields, duplicate keys,
trailing/malformed JSON, completion statuses, wrong types, mismatched IDs,
non-JSON/compressed responses, and HTTP authentication/errors. Receipts add only
message ID and idempotent; no arbitrary authority text enters results or errors.

The existing 20-second timeout, cancellation, redirect refusal, response/header
bounds, credential removal behavior and no-token-leakage tests remain intact.
No retry is added: the authority fingerprints the entire envelope, so a changed
timestamp under the same ID is a conflict, not a fresh task. Completion continues
through the dependency's existing `orchestration@gus` terminal consumer.
No result queue, PAM enablement, credentials, merge or deployment is included.

## Exact changed files

1. `runtimes/intern/dispatch.go` — production request and acknowledgement contract.
2. `runtimes/intern/dispatch_test.go` — updated wire/runtime tests and negative controls.
3. `docs/agentic/intern-bridge-client.md` — English configuration and protocol.
4. `docs/vi/agentic/intern-bridge-client_vi.md` — matching Vietnamese documentation.
5. `docs/receipts/intern-authority-2026-09-15.md` — this receipt.

## Runtime Proof

All commands below ran from the new worktree. Toolchain: `go version` returned
`go version go1.26.4 darwin/arm64`.

| Command | Observed result |
| --- | --- |
| `go test ./runtimes/intern/... ./system/lib/internbridge/...` | Exit 0 before edits and after the initial patch; all three packages passed. |
| `gofmt -w runtimes/intern/dispatch.go runtimes/intern/dispatch_test.go` | Exit 0, only changed Go files formatted. |
| `go test ./...` | Exit 0 on final code; all tested packages passed, others reported no test files. Intern 0.312s; server 8.351s; some unchanged packages used cache. |
| `go test -race ./runtimes/intern/... ./system/lib/internbridge/...` | Exit 0 on final code; all three packages passed. Intern 1.400s; other two cached from successful race run. |
| `gofmt -l runtimes/intern/dispatch.go runtimes/intern/dispatch_test.go` | Exit 0, empty output. |
| `git diff --check` | Exit 0, empty output. |

Focused proof covers exact envelopes with no body extras; both custody classes;
all five allowed intents and opposite-destination/invalid-intent controls;
2,000-byte ASCII and multibyte boundaries; missing/removed credentials;
wrong principals; enqueue/replay acknowledgement; every required response field;
extra/duplicate/trailing JSON; REPORTED_COMPLETE and delivered=true; cancellation,
timeout presence, HTTP authentication failures, content type/encoding/size,
redirect non-forwarding, token/error redaction, and owning-service queue receipts.
HTTP transports and loopback bridge servers are synthetic. No live authority,
consumer execution, device, model, or notification was invoked.

## Handoff and blockers

Implementation and local Go verification are complete. Live activation remains
blocked on dependency merge/deployment and independently provisioned principal
credentials; PAM is still disabled. No live end-to-end completion claim is made.
The Director reviewed the diff and explicitly authorized commit, push and PR
creation. The stacked PR targets `idirectships/autonomous-os` branch
`codex/gus-runtime-contract-20260914` at the recorded base commit. Upstream
`autonomous-ai/autonomous-os` PR #406 targets `main`, but its runtime head branch
exists only in the fork; targeting the fork branch isolates this integration.
All five files listed above are retained, including this receipt. Merge and
deployment remain outside the authorization.
No global Beads mutation was made under the worktree-only constraint; this
receipt retains the task evidence locally. Provider token/billing attribution
is unavailable for this task; no zero-spend claim is made.
