# Intern final-transcript admission contract — 2026-09-15

Origin: implementation@dru, Codex. Work remained in the assigned worktree on
`codex/gus-runtime-contract-20260914`.

## Result

The custom Intern OS server now defines
`POST /api/agent/intern/voice/transcript`. Admission requires the existing
signed administrator bearer session, ownership of an active bounded voice
grant, an exact final transcript assertion, reception operation, public or
business classification, and the exact case-insensitive `Gus` wake word.
Strict decoding rejects unknown, duplicate, case-variant, malformed, and
oversized JSON. Existing request validation bounds non-empty transcript text.
An explicit transcript attempt to grant restricted or secret access is rejected
before queueing.

Accepted work enters the existing Intern service through
`intern.AdmitTrustedVoiceRequest`, retaining grant cancellation and expiry. The
response is the existing asynchronous 202 contract: `run_id`, `state:queued`,
and `scope:bridge_request`. Existing bridge and provider guards preserve
final-answer-only output, Cassi-first reception metadata, and no execution.
Rejected fixture cases make no provider call.

## Scope boundary

This increment is an HTTP/software contract only. It does not connect physical
microphone capture, STT, HAL, TTS, sensing, device events, dispatch, services,
firmware, Wi-Fi, or credentials. No transcript is logged by the handler. A
future trusted native producer still has to supply this contract explicitly;
the endpoint does not infer classification from wake detection or transcript
content.

## Verification

Verification uses only local Go tests and synthetic HTTP providers. Exact final
results:

| Command | Observed result |
|---|---|
| `go test ./system/server -run 'TestIntern(FinalTranscriptAdmission\|VoiceGrantBoundary\|VoiceAdmissionRequiresBoundedGrant\|VoiceLifecycleCancelsGrant)$' -count=1` | Exit 0; focused server contract tests passed. |
| `go test -race -count=1 ./runtimes/intern/... ./system/lib/internbridge ./system/server` | Exit 0; all four relevant packages passed. |
| `go test ./system/server -count=1` | Exit 0; uncached server package passed. |
| `go test ./...` | Exit 0; full repository suite passed. |
| `go test -race ./...` | Exit 0; full repository race suite passed. |
| `go vet ./...` | Exit 0; no diagnostics. |
| `gofmt -l system/server/intern.go system/server/intern_voice.go system/server/intern_voice_test.go` | No output. |
| `git diff --check` | Exit 0; no diagnostics. |

The first `go test ./...` attempt encountered the repository's known shared
fixed-loopback-fixture timeout in `TestNativeInternLifecycleAndGeneration`;
every other package passed. The uncached server rerun, subsequent full suite,
and full race suite all passed, so this was a transient fixture collision rather
than a retained failure.

No hardware, live provider, merge, or deployment is part of this receipt.
