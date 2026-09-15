# Intern authenticated transcript grant — 2026-09-15

Origin: implementation@dru, Codex. Work stayed in the assigned Autonomous OS
worktree on `codex/gus-runtime-contract-20260914`, extending PR #406.

## Result and authority

The native Intern administrator router now admits microphone-derived text only
through an explicit, signed administrator session grant and an explicit
classification of each exact transcript. Default is disabled. Wake detection
does not grant trust. The existing session verifier is reused; the legacy
provider-key bearer fallback cannot mint or use a voice grant.

One ephemeral grant, bounded to 1–300 seconds and the authenticated session's
expiry, belongs to the existing router lifecycle. It stores only a token hash
and deadline/cancellation state. Replacement, DELETE revocation, expiry and
lifecycle shutdown invalidate it. Admission retains that lifetime in the
existing bounded queue. Invalidated queued work makes no provider request;
in-flight revocation cancels the HTTP wait and reports remote outcome unknown.

The server, client, bridge and provider retain their existing classification
validation: unknown/restricted/secret fail before inference. Exact transcripts
are not decorated with speaker identity, history or other context. Cassi-first
metadata, final-answer-only output, no peer dispatch, and bounded local-model
defaults remain in the existing bridge.

This addresses the administrator-reviewed transcript boundary from the earlier
voice blocker. It does not wire HAL, microphone capture, sensing or TTS. An
automatic trusted voice producer/classifier remains unimplemented. A trusted
administrator must use the voice endpoint for microphone-derived text; physical
origin cannot be inferred from arbitrary text passed to the existing typed-chat
API. Existing single-admin stateless sessions authenticate bearer possession,
not speaker identity; tokens with the same expiry are identical. Existing
administrator-wide result access and result TTL remain unchanged. Revocation
cannot undo a provider request already sent or prove remote computation stopped.

## Files

- `system/server/intern.go`: register voice routes and bind grant lifetime to
  existing native runtime cancellation.
- `system/server/intern_voice.go`: signed-session grant, revocation and exact
  transcript admission using existing strict request decoding.
- `runtimes/intern/service.go`: preserve grant deadline and cancellation across
  the existing queue and HTTP wait; require a bounded voice admission context.
- `system/server/intern_voice_test.go`: focused fixture acceptance and denial tests.
- `docs/agentic/intern.md`, `docs/vi/agentic/intern_vi.md`: matching API and limits.
- This receipt records verification; the 2026-09-14 blocker receipt is historical.

## Runtime Proof

Final code verification used local HTTP fixtures only, with synthetic session
material. No provider API, live model, device, credential file, Wi-Fi or service
was accessed or changed. No work occurred in Project Spider-Man or the live
runtime. No PR merge or deployment was authorized or performed.

| Command | Observed result |
|---|---|
| `go test ./system/server -run TestInternVoiceGrantBoundary -count=1` | Exit 0, server 1.593s (initial focused pass). |
| `go test -race -count=1 ./runtimes/intern/... ./system/lib/internbridge ./system/server` | Final pass exit 0: Intern 1.304s, bridge 1.649s, client 1.870s, server 9.443s. |
| `go vet ./runtimes/intern/... ./system/lib/internbridge ./system/server` | Exit 0, no diagnostics. |
| `go test ./system/server/sensing/delivery/http ./system/server/agent/delivery/http -count=1` | Exit 0, both packages passed. |
| `gofmt -l runtimes/intern/service.go system/server/intern.go system/server/intern_voice.go system/server/intern_voice_test.go` | Exit 0, no output. |
| `git diff --check` | Exit 0, no diagnostics. |

`TestInternVoiceGrantBoundary` verifies default disabled; wake-only denied;
legacy and absent authentication denied; invalid expiry denied; explicit
classification required; restricted, secret, unknown and empty classes denied
with zero provider calls; session/router isolation; one exact business turn
with Cassi-first final output and no credentials/history sent to the fixture;
queued revocation, expiry and replacement denied before provider; expiry capped
by session lifetime; and in-flight revocation propagated to the fixture.
`TestInternVoiceAdmissionRequiresBoundedGrant` denies nil, unbounded and canceled
grant contexts. `TestInternVoiceLifecycleCancelsGrant` proves shutdown disables
an existing grant. Existing runtime/bridge suites cover output filtering,
no fan-out, local defaults, queue/result bounds and minimal activation graph.

Full-repository tests/build and device acceptance were not rerun for this
focused change. Provider billing and Codex token/credit metrics are unavailable,
not reported as zero. Commit/push and PR status are reported in the task handoff.

## Refresh proof — 2026-09-15

`git fetch origin main` advanced `origin/main` from `86579cc3e` to
`d308e08db`. `git merge --no-edit origin/main` completed with no conflicts and
created merge commit `c4289522b`. The upstream commits touched harness,
hardware fallback, Buddy, MQTT/web and version files; no Intern grant conflict
was present. The grant gate, minimal Intern activation graph and provider
boundary remain in the merged tree.

The first parallel full/race invocation had a fixed-loopback-port contention
between independent server test processes and timed out in
`TestNativeInternLifecycleAndGeneration`; this was a test-runner collision, not
a code failure. The required checks were then rerun sequentially:

| Command | Observed result |
|---|---|
| `go test -race -count=1 ./runtimes/intern/... ./system/lib/internbridge ./system/server` | Exit 0; all four packages passed. |
| `go test ./...` | Exit 0; all packages passed. |
| `go vet ./...` | Exit 0, no diagnostics. |
| `gofmt -l $(git diff --name-only origin/main..HEAD -- '*.go')` | Exit 0, no output. |
| `git diff --check` | Exit 0, no diagnostics. |

## Final upstream refresh proof — 2026-09-15

After the first push, GitHub's standard `gh pr update-branch 406` operation
incorporated the then-current upstream base and created
`ba2868491` (`Merge branch 'main' into codex/gus-runtime-contract-20260914`).
`git fetch origin main` advanced the local base from `d308e08db` to
`aea3fafe7`, and `git merge --ff-only intern-pr-fork/codex-gus-runtime-20260914`
fast-forwarded the worktree to `ba2868491` with no conflicts. The merge commit's
second parent is `aea3fafe7`; `git merge-base --is-ancestor origin/main HEAD`
passed.

The additional upstream commit contains Buddy in-app update files only. It did
not alter Intern files or create an incompatibility. The final-head checks were
rerun sequentially because Intern tests use fixed loopback fixtures:

| Command | Observed result |
|---|---|
| `go test -race -count=1 ./runtimes/intern/... ./system/lib/internbridge ./system/server` | Exit 0; Intern 1.324s, bridge 1.652s, client 1.879s, server 9.426s. |
| `go test ./...` | Exit 0; all packages passed. |
| `go vet ./...` | Exit 0, no diagnostics. |
| `gofmt -l $(git diff --name-only origin/main..HEAD -- '*.go')` | Exit 0, no output. |
| `git diff --check` | Exit 0, no diagnostics. |

GitHub compare for the final pushed head reports `behind_by: 0` and
`ahead_by: 19`. PR #406 remains open and reports `mergeable: true` with
`mergeStateStatus: blocked`; its check rollup is empty, so no CI result is
available. The PR was not merged and no deployment occurred.

## Device voice lifecycle audit — 2026-09-15

Re-audited the active `agent_runtime: "intern"` lifecycle after the service
intent contract work. It does **not** admit a real microphone/STT request or
return speech through the device voice path, by deliberate design. This is a
safe blocker, not an incomplete wiring task.

- `system/server/intern.go:newInternServer` creates only the text-only gateway
  and the minimal Intern router. It constructs no sensing, agent-event, MQTT,
  HAL, or device handlers. `runIntern` starts the worker with a nil event
  handler, so no legacy or interim agent frame can enter a speech delivery path.
- The existing physical input surface,
  `system/server/sensing/delivery/http/handler.go:PostEvent`, rejects every
  `domain.IsTextOnlyGateway` before it binds or processes a `voice_command`.
  This prevents local intent/device execution as well as unclassified speech
  from reaching Intern.
- `SensingEventRequest` has no trusted `DataClass` or admission assertion for
  a transcript. Its `Message` is a plain string, while
  `intern.AdmitTrustedVoiceRequest` requires a bounded context and
  `internbridge.Request` with caller-supplied custody. Inferring public or
  business from STT text, a wake word, channel, or a model would violate the
  Intern boundary.
- The existing OS speech interface is `domain.AgentGateway.SendToHALTTS` (and
  `Speak`/queue variants), but `runtimes/intern/unsupported.go` intentionally
  returns `domain.ErrNotSupportedByRuntime` for each. The administrator-only
  voice grant routes only to the bounded bridge/result API and never to TTS.

Required Director/upstream confirmation: define an authenticated HAL-to-server
voice admission interface that carries an exact final transcript, explicit
trusted custody class, bounded capture/grant lifetime, and interaction ID;
define its final-only result ownership for the existing HAL speech endpoint.
The contract must say how interim/STT legacy frames, mixed or unknown custody,
and smart-home phrases are rejected before bridge submission and how a final
answer is distinguished from reception/service proposals. No such interface
exists today. Adding a sensing callback, assigning a default data class, or
calling HAL directly would invent authority and would make the text-only
Intern runtime a device-control plane, so this PR does not do so.

Focused regression proof now includes an Intern-selected request to
`/api/sensing/event` carrying a `voice_command`; it remains `501 Not
Supported`, alongside device/channel/action and unclassified-input denials.
