# Saved selection versus active gateway: repair proof

Date: 2026-09-14. Origin: scoped repair worker@dru, Codex.

Scope: local checkout repair and tests only; no commit, branch switch/reset,
deployment, live service change, PSM, firmware, or credential edits.
The checkout already contained an Intern draft and continued receiving other
draft changes; the file list below identifies this repair's contributions.

## Result and audit

At inspection, `device.Service.externalRuntime()` already returned only
`domain.IsTextOnlyGateway(s.agentGateway)`. Its contract is now documented and
covered by regression tests. All nine production call sites were inspected:

- Effect guards: `AddMCPTool`, `RemoveMCPTool`, `Setup`, `ReprovisionWifi`,
  `UpdateConfig`, `UpdateVoiceConfig`, `RestoreAutonomousDefaults`, and
  `UpdateRealtimeConfig` use only the instantiated gateway through this helper.
- `updateAgentRuntime` is an activation boundary. It separately checks the
  requested and saved selection, in addition to active ownership, to prevent
  automatic transitions across the Intern boundary. Its saved-selection read
  now tolerates nil config, preserving invalid-request validation in the
  existing switch reservation test. The old runtime passed to switching comes
  from the active gateway when present.
- `SelectExternalRuntime`, the HTTP runtime selector, and
  `RestartForAgentRuntime` retain separate selection-boundary checks.

Channel, MCP, and LLM config reconciliation now derive their current runtime
and markers from the instantiated gateway and skip an active Intern gateway.
They no longer read a concurrently changing saved runtime field.

Server log lookup, agent operation targets, runtime status, MQTT info, and
device ping use the active runtime. Server and device lookup fall back to the
saved/default runtime only when no gateway exists; absent config falls back to
OpenClaw. Original nil-gateway log fixtures remain intact, with separate tests
covering an active Codex gateway while Intern is selected.

The config-only alert metadata reader now uses the locked selection accessor.
Remaining direct `Config.AgentRuntime` reads in the gateway factory and default
seeding occur during startup before request/listener concurrency. Config's own
accessor and selection writes are locked; runtime mutations use locked save
callbacks. The bootstrap and web-CLI helpers decode their own local config
structures, rather than reading the live server's shared config. They were
inspected and left unchanged.

Sensing and config-listener restrictions were already based on the active
gateway. New sensing tests prove that queued events and the realtime-handled
hook continue before restart, and neither executes on an active Intern gateway.

## Files changed by this repair

Production:

- `system/device/runtime_external.go` — document the active-only effect guard.
- `system/device/runtime.go` — nil-safe selection guard/default lookup, active
  old-runtime lookup, and selection-boundary documentation.
- `system/device/status_reporter.go` — report active runtime.
- `system/agent/channel_reconcile.go` — active runtime and Intern restriction.
- `system/agent/config_migration.go` — active runtime and Intern restriction.
- `system/agent/mcp_reconcile.go` — active runtime and Intern restriction.
- `system/lib/alert/alert.go` — lock the saved-selection read.
- `system/server/runtime.go` — active lookup with nil-safe fallback.
- `system/server/logs.go` — active log target with fallback.
- `system/server/system_ops.go` — active agent operation targets.
- `system/server/server.go` — active runtime checks/status.
- `system/server/device/delivery/mqtt/info_handler.go` — active runtime info
  with a fallback when no gateway exists.

Tests:

- `system/device/runtime_external_test.go` — channel/config behavior, all eight
  active-Intern effect guards, nil fallback, concurrent selection/config updates.
- `system/agent/runtime_selection_test.go` — markers follow active runtime,
  active Intern remains restricted, concurrent selection/reconciliation.
- `system/agent/channel_reconcile_test.go` — give the fake its actual runtime.
- `system/agent/mcp_reconcile_test.go` — give the fake its actual runtime.
- `system/server/sensing/delivery/http/runtime_selection_test.go` — sensing
  admission, queue, and hook behavior before restart in both directions.
- `system/server/logs_source_test.go` — preserve original nil-gateway tests;
  add active-versus-selected and nil-fallback tests.

Documentation:

- `docs/agentic/intern.md`
- `docs/vi/agentic/intern_vi.md`
- `docs/agentic/runtime-selection-repair-proof.md` (this receipt)

## Runtime Proof

Focused regression tests passed with the race detector. Then the complete test
suites for all five affected packages passed, both normally and under `-race`:

```sh
go test -mod=readonly ./system/device ./system/agent ./system/server/config ./system/server/sensing/delivery/http ./system/server
go test -mod=readonly -race ./system/device ./system/agent ./system/server/config ./system/server/sensing/delivery/http ./system/server -count=1
```

Final race run output:

```text
ok  go.autonomous.ai/os/system/device                         1.752s
ok  go.autonomous.ai/os/system/agent                          1.916s
ok  go.autonomous.ai/os/system/server/config                  1.774s
ok  go.autonomous.ai/os/system/server/sensing/delivery/http    1.948s
ok  go.autonomous.ai/os/system/server                         8.180s
```

This includes the existing nil-config switch reservation test, original
nil-gateway log tests, minimal cold-start Intern graph, restricted Intern HTTP
surface, and the new concurrent saved-versus-active regression tests.

`gofmt` ran on all 18 Go files touched by this repair; `gofmt -l` returned no
paths. `git diff --check` returned no diagnostics. Tests use temporary config
files and fake gateways; the existing bridge test uses a local test HTTP server.
No repository-wide `go test ./...` or hardware/deployment verification was run.
