# Intern voice integration contract stop — 2026-09-14

Outcome: **voice integration blocked; no runtime implementation or new tests
added**. This records the requested stop-before-stub outcome, not completion
of a voice agent. Origin: codex@dru, using Codex.

## Source and merge

Work stayed in the assigned isolated checkout on
`codex/gus-runtime-contract-20260914`.
`git fetch origin` and `git merge --no-edit origin/main` succeeded without
conflicts. Fetched main: `86579cc3ec64279d2c56893b42356fa4ea826923`.
Merge commit: `93719fe7faeb876d0cd52f9409110a9dde9719df`.
No reset, force push, live-tree branch switch, deployment, or device access.

## Exact missing contract

An existing trusted voice admission authority must classify the exact input
as public/business before Intern consumes it, with a decision bound to that
payload across queueing and any transformation. The current implementation
has only an authenticated administrator's explicit per-body assertion. No
equivalent voice producer/policy is defined. Wake admission is permission to
address the device, not evidence that speech contains no restricted or secret
data. Local inference does not waive the bridge's admission rules; explicit
Cerebras selection does not classify incoming speech either.

Required Director-owned decision: name the trusted voice classifier/policy
and its payload boundary, including treatment of speaker identity, context,
unclassified input, and remote-provider eligibility. This is a custody-policy
decision, not another request for approval to implement the already assigned
integration. No new classifier, default data class, or bypass was invented.

## File evidence at the merge commit

| Boundary | Existing code and consequence |
|---|---|
| Startup | `system/server/intern.go:33` selects the minimal graph; `newInternServer` at line 58 constructs no sensing/agent event handler. `runIntern` at line 266 calls `StartWS(ctx, nil)`. `system/server/server.go:216` returns through `serveIntern` before normal device startup. |
| HAL payload | `hal/drivers/voice/_internal/sensing_sender.py:116` creates type/message JSON. Wake metadata at line 118 is explicitly observational. The sender adds resolved `current_user`, optional images, and logs the payload; it has no trusted data-class assertion. `turn_dispatch.py:208` classifies wake words; the normal path also handles speaker decoration and realtime context. |
| Sensing admission | `system/server/sensing/delivery/http/handler.go:89` defines no data-class/admission evidence. `PostEvent:218` rejects Intern before binding. The normal path logs text at line 243 and augments it through `sensingmsg.Build` at line 824. `system/server/server.go:467` uses `sameOriginOrLAN`, which establishes location/origin, not classification. |
| Bridge admission | `system/server/intern.go:194` requires authenticated administrator classification of exact text. `runtimes/intern/service.go:40` validates trusted admissions; generic send methods return `ErrNeedsClassification`. `system/lib/internbridge/client.go:161` rejects unknown/restricted/secret classes before networking. |
| Reply/TTS | `runtimes/intern/service.go` ignores the event callback and stores results for polling. `system/server/agent/delivery/http/handler_events.go:44` rejects Intern events. `runtimes/intern/unsupported.go:136` rejects TTS. The shared agent event implementation can invoke `fireHWCallsSync` (`handler_event_agent.go:936`); removing the broad text-only gate alone would not preserve the requested no-tool/no-hardware-effects boundary. |
| Hardware and wake | `robots/intern-v2/ROBOT.md` declares shared audio/voice and optional sensing, but is not a runtime admission policy. HAL wake words derive from agent/device names (`hal/app_state.py:1652`, `hal/drivers/voice/_internal/config.py:201`), while the bridge's Cassi-first reception metadata does not configure or authenticate microphone wake admission. No live GUSystems/Gus wake configuration was inspected or changed. |

Once admission is established, the remaining wiring is implementable in the
existing server, SensingHandler, Service queue, and AgentHandler/TTS contracts:
construct the required handlers without unrelated startup effects; preserve
admission and correlation; deliver only a validated final reply through the
existing TTS handling; gate tools, images, channels, passive execution and
replay; and make readiness/cancellation truthful and bounded. No second bus,
router, service, store, or device-control API is required. Missing optional
tools or sessions is not the reason for this stop.

## Runtime Proof

All commands ran locally against the merged checkout. Fixture providers were
used; no live model, microphone, speaker, or remote provider was exercised.

| Command | Observed result |
|---|---|
| `go test ./runtimes/intern/... ./system/lib/internbridge ./system/server -run 'Test(Intern\|NativeIntern\|InitializeIntern\|Admission\|ChatQueue\|WaitStarted\|Failures\|Offline\|Queue)' -count=1` | Exit 0. Bridge/client packages had no matching names in this filtered invocation; the unfiltered command below covers them. |
| `go test ./runtimes/intern/... ./system/lib/internbridge -count=1` | Exit 0; all three packages passed uncached. |
| `go test ./system/server/sensing/delivery/http -run TestRuntimeSelectionPreservesActiveSensing -count=1` | Exit 0. Confirms active Intern rejects sensing; this is negative evidence, not voice acceptance. |
| `go test ./...` | Exit 0; all packages passed, with cached results where Go reported them. |
| `go vet ./...` | Exit 0, no diagnostics. |
| `make os-build` | Exit 0; Linux ARM64 os-server built locally; binary not committed. |
| `go test -race ./runtimes/intern/... ./system/lib/internbridge ./system/server ./system/server/sensing/delivery/http ./system/server/agent/delivery/http ./runtimes/hermes` | Exit 0; all seven packages passed. Includes both packages changed by the upstream merge. |

`gofmt -l` was run on `runtimes/intern`, `system/lib/internbridge`, the three
Intern server files, and every Go file changed by the merge. It reported only
`runtimes/hermes/chat.go`, inherited upstream formatting left untouched under
the repository's unrelated-formatting rule. No new Go code needs formatting.

Existing tests verify admin request/provider/result correlation, blocked
images/tools/channels, bounded queue/result expiry, cancellation, and readiness.
`TestInitializeInternServerUsesMinimalActivationGraph` explicitly verifies
that sensing and agent handlers are absent. These tests therefore cannot
honestly prove the requested microphone → Intern → final TTS path. No fake
positive voice test was added. Full voice acceptance remains outstanding.

Local test timings are available in command output; provider billing/token
usage was unavailable. No billing or zero-usage claim is made. The existing
work-tracking epic was inspected read-only; live-tree ledgers were not changed.
