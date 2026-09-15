# Native Intern bridge verification — 2026-09-14

Origin: implementation@dru · tool: Codex · local repository verification.
Worktree: `/Users/dru/DEV/autonomous-os-intern-native-bridge-20260914`.
Branch: `codex/intern-native-bridge-20260914`.
Toolchain: `go version go1.26.4 darwin/arm64`.

## Result

Completed the inherited native Go bridge inside the existing Intern os-server
process. Changes remain uncommitted and unstaged for review.

- Ollama remains the default, restricted to device loopback. Explicit Cerebras
  selection consumes the existing configured HTTPS base, model and key.
  Generic OpenAI selection now fails startup. No automatic provider fallback.
- One Cassi-first reception proposal is computed before generation and retained
  on completion. At most one provider call; peer names are metadata, never network
  destinations. Explicit home/service routes do not call the model.
- Complete assistant content only. Reject thinking/tool metadata, XML/bracket
  markers, hardware link syntax, reasoning labels/headings/fences, speech
  wrappers and runtime sentinels. Rejected content becomes the fixed fallback;
  it is never partially stripped into a successful answer.
- Ollama token/output ratio now uses trimmed output, closing whitespace padding.
- English and Vietnamese configuration, lifecycle and limitations are documented.

## Regression proof

The inherited focused package checks passed before changes. The added command
`go test ./runtimes/intern/bridge -run 'TestProvider(Configuration|FinalAnswerBoundary|WhitespaceCannotHideTokenBurn)$'`
then failed with exit 1: generic OpenAI configuration was accepted, all 28
non-final-output cases escaped (14 each for Ollama/Cerebras), and whitespace
padding hid excessive token use. These regression tests pass in the full and
fresh race runs below.

Additional fixture coverage verifies Cerebras truncation, reasoning/tool
metadata, role/content shape, multiple choices, ordinary final text, one provider
call despite multiple employee names, fixed fallback output, and startup
configuration isolation. Existing lifecycle tests verify queue-to-bridge-to-model
completion, busy ports, inactive runtimes, shutdown cancellation and socket release.

## Runtime Proof

| Command | Exact result |
|---|---|
| `go test ./...` | Exit 0; 54 packages pass (4 cached), 21 report no test files. |
| `go vet ./...` | Exit 0; no output or diagnostics. |
| `make os-build` | Exit 0; Linux ARM64 os-server built successfully. |
| `go test -race -count=1 ./runtimes/intern/... ./system/lib/internbridge ./system/server` | Exit 0; all 4 packages pass freshly with the race detector. |
| `git diff --check` | Exit 0; no diagnostics. |
| `gofmt -l runtimes/intern/bridge system/server/intern.go system/server/intern_bridge_test.go system/server/config/config.go` | Exit 0; no output. |
| `file system/os-server` | ELF 64-bit LSB executable, ARM aarch64, statically linked, stripped. |
| `git check-ignore system/os-server` | Exit 0; prints `system/os-server`. Build artifact is ignored. |

### Full repository test output

```text
ok  	go.autonomous.ai/os/runtimes/claudecode	0.772s
ok  	go.autonomous.ai/os/runtimes/claudecode/gatewayd	1.724s
ok  	go.autonomous.ai/os/runtimes/codex	0.643s
ok  	go.autonomous.ai/os/runtimes/codex/gatewayd	1.272s
ok  	go.autonomous.ai/os/runtimes/hermes	0.366s
ok  	go.autonomous.ai/os/runtimes/intern	(cached)
ok  	go.autonomous.ai/os/runtimes/intern/bridge	0.639s
ok  	go.autonomous.ai/os/runtimes/openclaw	0.623s
ok  	go.autonomous.ai/os/runtimes/opencode	4.696s
ok  	go.autonomous.ai/os/runtimes/opencode/gatewayd	1.374s
ok  	go.autonomous.ai/os/runtimes/picoclaw	0.478s
ok  	go.autonomous.ai/os/system/agent	1.068s
ok  	go.autonomous.ai/os/system/agent/migrate_config	0.207s
ok  	go.autonomous.ai/os/system/agent/migrate_persona	0.346s
ok  	go.autonomous.ai/os/system/agentfile	0.410s
ok  	go.autonomous.ai/os/system/ambient	0.266s
ok  	go.autonomous.ai/os/system/beclient	0.188s
ok  	go.autonomous.ai/os/system/bootstrap	0.380s
?   	go.autonomous.ai/os/system/bootstrap/config	[no test files]
ok  	go.autonomous.ai/os/system/bootstrap/state	0.186s
ok  	go.autonomous.ai/os/system/buddy	0.322s
?   	go.autonomous.ai/os/system/cmd/bootstrap	[no test files]
ok  	go.autonomous.ai/os/system/cmd/os-server	0.394s
ok  	go.autonomous.ai/os/system/device	1.668s
ok  	go.autonomous.ai/os/system/domain	0.302s
ok  	go.autonomous.ai/os/system/environment	9.285s
ok  	go.autonomous.ai/os/system/externalhistory	1.562s
ok  	go.autonomous.ai/os/system/harness	0.796s
?   	go.autonomous.ai/os/system/healthwatch	[no test files]
ok  	go.autonomous.ai/os/system/intent	0.280s
ok  	go.autonomous.ai/os/system/lib/alert	0.249s
ok  	go.autonomous.ai/os/system/lib/analytics	0.246s
?   	go.autonomous.ai/os/system/lib/core/system	[no test files]
?   	go.autonomous.ai/os/system/lib/flow	[no test files]
ok  	go.autonomous.ai/os/system/lib/hal	0.206s
ok  	go.autonomous.ai/os/system/lib/i18n	0.190s
ok  	go.autonomous.ai/os/system/lib/internbridge	(cached)
ok  	go.autonomous.ai/os/system/lib/logger	0.261s
?   	go.autonomous.ai/os/system/lib/mqtt	[no test files]
?   	go.autonomous.ai/os/system/lib/osreset	[no test files]
?   	go.autonomous.ai/os/system/lib/runtimereg	[no test files]
?   	go.autonomous.ai/os/system/lib/safego	[no test files]
ok  	go.autonomous.ai/os/system/lib/sensingmsg	0.208s
ok  	go.autonomous.ai/os/system/lib/speakergate	0.452s
ok  	go.autonomous.ai/os/system/lib/syspath	0.157s
ok  	go.autonomous.ai/os/system/lib/urlnorm	0.153s
ok  	go.autonomous.ai/os/system/lib/usercanon	0.236s
ok  	go.autonomous.ai/os/system/lib/versioncache	1.196s
ok  	go.autonomous.ai/os/system/monitor	0.248s
ok  	go.autonomous.ai/os/system/network	0.272s
?   	go.autonomous.ai/os/system/plugin	[no test files]
ok  	go.autonomous.ai/os/system/schedule	0.802s
ok  	go.autonomous.ai/os/system/server	(cached)
ok  	go.autonomous.ai/os/system/server/agent/delivery/http	0.458s
ok  	go.autonomous.ai/os/system/server/buddy/delivery/http	0.369s
ok  	go.autonomous.ai/os/system/server/config	(cached)
ok  	go.autonomous.ai/os/system/server/device/delivery/http	0.331s
ok  	go.autonomous.ai/os/system/server/device/delivery/mqtt	0.819s
?   	go.autonomous.ai/os/system/server/health/delivery/http	[no test files]
?   	go.autonomous.ai/os/system/server/network/delivery/http	[no test files]
?   	go.autonomous.ai/os/system/server/plugin/delivery/http	[no test files]
ok  	go.autonomous.ai/os/system/server/sensing/delivery/http	0.936s
?   	go.autonomous.ai/os/system/server/serializers	[no test files]
?   	go.autonomous.ai/os/system/server/session	[no test files]
ok  	go.autonomous.ai/os/system/server/system	0.268s
ok  	go.autonomous.ai/os/system/server/telemetry/delivery/http	0.340s
?   	go.autonomous.ai/os/system/skillcontext	[no test files]
?   	go.autonomous.ai/os/system/skillcontext/mood	[no test files]
?   	go.autonomous.ai/os/system/skillcontext/musicsuggestion	[no test files]
?   	go.autonomous.ai/os/system/skillcontext/posture	[no test files]
?   	go.autonomous.ai/os/system/skillcontext/wellbeing	[no test files]
ok  	go.autonomous.ai/os/system/skills	0.447s
?   	go.autonomous.ai/os/system/statusled	[no test files]
ok  	go.autonomous.ai/os/system/telemetry	0.342s
ok  	go.autonomous.ai/os/system/vision	0.189s
```

### Build output

```text
cd system && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w -X go.autonomous.ai/os/system/server/config.OSVersion=v0.1.8-420-gf6dfd71d6-dirty" -o os-server ./cmd/os-server
```

### Fresh race output

```text
ok  go.autonomous.ai/os/runtimes/intern                 1.325s
ok  go.autonomous.ai/os/runtimes/intern/bridge          1.705s
ok  go.autonomous.ai/os/system/lib/internbridge         1.890s
ok  go.autonomous.ai/os/system/server                  8.283s
```

## Boundaries and remaining deployment proof

Provider tests use local HTTP/TLS fixtures and dummy configuration values. No
live Ollama or Cerebras request was made; real model availability, latency,
billing, and provider behavior on a device remain unverified. Marker rejection
and the token ratio are conservative checks, not proof of the semantic absence
of unmarked reasoning or a provider's internal reasoning behavior.

No firmware, Wi-Fi, credential files, live services, other runtime defaults,
or unrelated repository files were changed. No device deployment, new service,
second runtime, commit or push occurred. The ignored build output remains at
`system/os-server`.

No live inference usage was incurred by the verification fixtures. Codex session
token/credit accounting was not exposed by the execution tools and is unavailable;
it is not recorded as zero.

Configuration and bounds: [English](../agentic/intern-native-bridge.md) ·
[Vietnamese](../vi/agentic/intern-native-bridge_vi.md).
