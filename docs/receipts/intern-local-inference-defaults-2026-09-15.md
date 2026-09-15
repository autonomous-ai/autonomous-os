# Intern local inference compatibility — 2026-09-15

Origin: engineering@dru, Codex tool session.
Workspace: `/Users/dru/DEV/autonomous-os-gus-runtime-20260914`.
Branch: `codex/gus-runtime-contract-20260914`.
Base: `b5969f40204347a3da8c967865626e9e41942344`.
PR: https://github.com/autonomous-ai/autonomous-os/pull/406.

## Decision and evidence

Preserve provider source and tests exactly as at the base commit. Device
defaults remain `http://127.0.0.1:11434` and `qwen3:4b`.

- `robots/intern-v2/README.md` declares 4 GB RAM and a BCM2712 processor.
- Original bridge commit `ca18d052c` explicitly sets loopback port 11434 and
  `qwen3:4b`; its constructor comment calls these device-local defaults.
- The corresponding native-bridge docs and original receipt describe
  device-loopback inference. This establishes an intentional existing default,
  but does not establish measured 4B memory fit or explain the model-size choice.
- The Director reports `qwen3:14b` verified only on the GUS Mac. That proof
  does not establish Intern hardware compatibility.
- `system/server/intern.go` consumes `InternOllamaURL` and `InternOllamaModel`
  explicitly. `NewProvider` rejects non-loopback Ollama hosts. Therefore an
  explicit GUS-local configuration is supported; device-to-GUS inference is not.

The interrupted 14B default change was never committed or pushed. Its source
and test edits were undone. The final patch only documents the existing
GUS-hosted override in English/Vietnamese and records this compatibility review.
The example is for an Intern server on GUS itself, not a remote device route.

No credentials, network fallback, device selection, firmware, launchd,
deployment, daemon settings, or service-dispatch semantics changed.
`think:false`, `keep_alive:5m`, concurrency limits and existing one-model
constraints remain unchanged. No live hardware or model calls were performed.

## Runtime Proof

The following commands exited 0 after restoring the source and tests. Synthetic
tests verify configuration and runtime contracts, not hardware compatibility.
Codex token/provider billing accounting was unavailable, not measured as zero.

```text
env -u BYO_LIVE_URL GOPROXY=off GOSUMDB=off go test -race -count=1 ./runtimes/intern/bridge ./runtimes/intern ./system/lib/internbridge
ok go.autonomous.ai/os/runtimes/intern/bridge 1.787s
ok go.autonomous.ai/os/runtimes/intern 1.277s
ok go.autonomous.ai/os/system/lib/internbridge 1.836s

env -u BYO_LIVE_URL GOPROXY=off GOSUMDB=off go test -count=1 ./system/server -run 'Test(NativeIntern|Intern)'
ok go.autonomous.ai/os/system/server 1.663s

git diff --exit-code HEAD -- runtimes/intern/bridge/provider.go runtimes/intern/bridge/provider_test.go
(no output; source and tests match the base commit)

gofmt -l runtimes/intern/bridge/provider.go runtimes/intern/bridge/provider_test.go
(no output)

git diff --check
(no output)
```

The Intern package run includes service-dispatch regressions. Only the English
and Vietnamese native-bridge docs and this receipt are included in the commit.
