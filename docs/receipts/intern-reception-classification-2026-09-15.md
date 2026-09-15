# Native Intern ordinary-language reception — 2026-09-15

Origin: engineering@dru, Codex tool session. Tracking: `dru-q42wo`.
Workspace: `/Users/dru/DEV/autonomous-os-gus-runtime-20260914`.
Initial scope: uncommitted software increment; no deployment, commit, or merge.
Follow-up: Director authorized full repository verification and a local commit
on the existing feature branch. No push, merge, or deployment is authorized.

## Contract and implementation

The existing `reception` operation now classifies unresolved public/business
requests using at most one bounded call to the existing local Ollama provider.
The label allowlist follows the bounded Python contract inspected at
`/Users/dru/DEV/project-spider-man-welcome-desk-mainline-2026-09-15/packages/agents/src/gus/agents/intern.py`.
No caller API, protocol version, provider configuration, or lifecycle changes
were needed. Classification labels are proposals only, never custody authority.

Explicit internal addresses and deterministic service routes remain model-free.
Home-keyword and caller-custody holds precede inference. Mixed content/PAM phrases
clarify without inference. Model-classified home/reception intents also hold.
Malformed/ambiguous output, unknown labels, and provider failures produce fixed
clarification with unknown intent and null handoff. No raw model prose or
reasoning is returned. The existing output, token-ratio, concurrency, cancellation,
timeout and transport checks remain in force.

Cerebras `generate`/`classify` behavior remains unchanged. The new reception
classification path is local-only; explicit Cerebras returns clarification for
unresolved reception without network I/O or creating another provider.
Every response preserves Cassi-first metadata, `executed:false`,
`executes_actions:false`, `next_step:safe_escalation`, and one proposal maximum.

## Exact changed files

- `runtimes/intern/bridge/server.go`
- `runtimes/intern/bridge/provider.go`
- `runtimes/intern/bridge/server_test.go`
- `runtimes/intern/bridge/reception_test.go` (new)
- `docs/agentic/intern-native-bridge.md`
- `docs/vi/agentic/intern-native-bridge_vi.md`
- `docs/receipts/intern-reception-classification-2026-09-15.md` (this receipt)

## Runtime Proof

Commands ran from the workspace above. All exited 0.

```text
gofmt -w runtimes/intern/bridge/server.go runtimes/intern/bridge/provider.go runtimes/intern/bridge/server_test.go runtimes/intern/bridge/reception_test.go
(no output)

go test ./runtimes/intern/bridge ./runtimes/intern ./system/lib/internbridge
ok  	go.autonomous.ai/os/runtimes/intern/bridge	0.375s
ok  	go.autonomous.ai/os/runtimes/intern	(cached)
ok  	go.autonomous.ai/os/system/lib/internbridge	(cached)

go test -race -count=1 ./runtimes/intern/bridge ./runtimes/intern ./system/lib/internbridge
ok  	go.autonomous.ai/os/runtimes/intern/bridge	1.884s
ok  	go.autonomous.ai/os/runtimes/intern	1.430s
ok  	go.autonomous.ai/os/system/lib/internbridge	2.025s

gofmt -l runtimes/intern/bridge/server.go runtimes/intern/bridge/provider.go runtimes/intern/bridge/server_test.go runtimes/intern/bridge/reception_test.go
(no output)

git diff --check
(no output)
```

Synthetic HTTP/TLS fixtures cover ordinary briefing/news, alarm/notification,
all accepted labels, explicit personas, pre-inference and classified custody
holds, ambiguous/malformed output, provider errors, redirects, reasoning metadata,
excess token consumption, truncation, busy/canceled inference, zero-call Cerebras
reception, and compatibility with the existing strict client.

No live model calls were made. Synthetic labels prove the contract and call
bounds, not model accuracy or production availability. No firmware, device state,
Wi-Fi, credentials, notification recipients, pairing, live services, or unrelated
packages were changed. Provider billing and Codex token accounting were unavailable;
neither is reported as zero.

## Full repository verification before commit

Reviewed all seven changed files, including the new tests and this receipt.
Changes remain limited to the existing bridge/provider, synthetic tests, and
English/Vietnamese documentation. No blocking diff findings or unrelated edits.
Branch: `codex/gus-runtime-contract-20260914`.

The live endpoint opt-in `BYO_LIVE_URL` was removed from the command environment;
module proxy and checksum network lookups were disabled. No live-service tests
were enabled. Commands below exited 0. The race run reported 54 passing packages
(including cached results) and 21 packages with no test files.

```text
env -u BYO_LIVE_URL GOPROXY=off GOSUMDB=off go test -race ./...
ok  	go.autonomous.ai/os/runtimes/claudecode	(cached)
ok  	go.autonomous.ai/os/runtimes/claudecode/gatewayd	(cached)
ok  	go.autonomous.ai/os/runtimes/codex	(cached)
ok  	go.autonomous.ai/os/runtimes/codex/gatewayd	(cached)
ok  	go.autonomous.ai/os/runtimes/hermes	(cached)
ok  	go.autonomous.ai/os/runtimes/intern	(cached)
ok  	go.autonomous.ai/os/runtimes/intern/bridge	1.796s
ok  	go.autonomous.ai/os/runtimes/openclaw	(cached)
ok  	go.autonomous.ai/os/runtimes/opencode	(cached)
ok  	go.autonomous.ai/os/runtimes/opencode/gatewayd	(cached)
ok  	go.autonomous.ai/os/runtimes/picoclaw	(cached)
ok  	go.autonomous.ai/os/system/agent	(cached)
ok  	go.autonomous.ai/os/system/agent/migrate_config	(cached)
ok  	go.autonomous.ai/os/system/agent/migrate_persona	(cached)
ok  	go.autonomous.ai/os/system/agentfile	(cached)
ok  	go.autonomous.ai/os/system/ambient	(cached)
ok  	go.autonomous.ai/os/system/beclient	(cached)
ok  	go.autonomous.ai/os/system/bootstrap	(cached)
?   	go.autonomous.ai/os/system/bootstrap/config	[no test files]
ok  	go.autonomous.ai/os/system/bootstrap/state	(cached)
ok  	go.autonomous.ai/os/system/buddy	(cached)
?   	go.autonomous.ai/os/system/cmd/bootstrap	[no test files]
ok  	go.autonomous.ai/os/system/cmd/os-server	1.307s
ok  	go.autonomous.ai/os/system/device	(cached)
ok  	go.autonomous.ai/os/system/domain	(cached)
ok  	go.autonomous.ai/os/system/environment	(cached)
ok  	go.autonomous.ai/os/system/externalhistory	(cached)
ok  	go.autonomous.ai/os/system/harness	(cached)
?   	go.autonomous.ai/os/system/healthwatch	[no test files]
ok  	go.autonomous.ai/os/system/intent	(cached)
ok  	go.autonomous.ai/os/system/lib/alert	(cached)
ok  	go.autonomous.ai/os/system/lib/analytics	(cached)
?   	go.autonomous.ai/os/system/lib/core/system	[no test files]
?   	go.autonomous.ai/os/system/lib/flow	[no test files]
ok  	go.autonomous.ai/os/system/lib/hal	(cached)
ok  	go.autonomous.ai/os/system/lib/i18n	(cached)
ok  	go.autonomous.ai/os/system/lib/internbridge	(cached)
ok  	go.autonomous.ai/os/system/lib/logger	(cached)
?   	go.autonomous.ai/os/system/lib/mqtt	[no test files]
?   	go.autonomous.ai/os/system/lib/osreset	[no test files]
?   	go.autonomous.ai/os/system/lib/runtimereg	[no test files]
?   	go.autonomous.ai/os/system/lib/safego	[no test files]
ok  	go.autonomous.ai/os/system/lib/sensingmsg	(cached)
ok  	go.autonomous.ai/os/system/lib/speakergate	(cached)
ok  	go.autonomous.ai/os/system/lib/syspath	(cached)
ok  	go.autonomous.ai/os/system/lib/urlnorm	(cached)
ok  	go.autonomous.ai/os/system/lib/usercanon	(cached)
ok  	go.autonomous.ai/os/system/lib/versioncache	(cached)
ok  	go.autonomous.ai/os/system/monitor	(cached)
ok  	go.autonomous.ai/os/system/network	(cached)
?   	go.autonomous.ai/os/system/plugin	[no test files]
ok  	go.autonomous.ai/os/system/schedule	(cached)
ok  	go.autonomous.ai/os/system/server	9.388s
ok  	go.autonomous.ai/os/system/server/agent/delivery/http	(cached)
ok  	go.autonomous.ai/os/system/server/buddy/delivery/http	(cached)
ok  	go.autonomous.ai/os/system/server/config	(cached)
ok  	go.autonomous.ai/os/system/server/device/delivery/http	(cached)
ok  	go.autonomous.ai/os/system/server/device/delivery/mqtt	(cached)
?   	go.autonomous.ai/os/system/server/health/delivery/http	[no test files]
?   	go.autonomous.ai/os/system/server/network/delivery/http	[no test files]
?   	go.autonomous.ai/os/system/server/plugin/delivery/http	[no test files]
ok  	go.autonomous.ai/os/system/server/sensing/delivery/http	(cached)
?   	go.autonomous.ai/os/system/server/serializers	[no test files]
?   	go.autonomous.ai/os/system/server/session	[no test files]
ok  	go.autonomous.ai/os/system/server/system	(cached)
ok  	go.autonomous.ai/os/system/server/telemetry/delivery/http	(cached)
?   	go.autonomous.ai/os/system/skillcontext	[no test files]
?   	go.autonomous.ai/os/system/skillcontext/mood	[no test files]
?   	go.autonomous.ai/os/system/skillcontext/musicsuggestion	[no test files]
?   	go.autonomous.ai/os/system/skillcontext/posture	[no test files]
?   	go.autonomous.ai/os/system/skillcontext/wellbeing	[no test files]
ok  	go.autonomous.ai/os/system/skills	(cached)
?   	go.autonomous.ai/os/system/statusled	[no test files]
ok  	go.autonomous.ai/os/system/telemetry	(cached)
ok  	go.autonomous.ai/os/system/vision	(cached)

env -u BYO_LIVE_URL GOPROXY=off GOSUMDB=off go vet ./...
(no output)

gofmt -l runtimes/intern/bridge/server.go runtimes/intern/bridge/provider.go runtimes/intern/bridge/server_test.go runtimes/intern/bridge/reception_test.go
(no output)

git diff --check
(no output)
```
