# Provider usage

The desktop footer shows provider-reported subscription usage for Claude and Codex. Each available window displays percentage used and time until reset; unavailable data never becomes a fabricated zero. The footer refreshes on mount and every 60 seconds, supports manual refresh, and does not overlap its requests.

## Contract

The validated preload method `providerUsage(refresh?: boolean)` returns `ProviderUsage[]`: `provider` (`claude` or `codex`), `state` (`ready`, `unavailable`, or `error`), optional `message`, `windows` (`label`, `usedPercent`, optional `resetsAt`), and `updatedAt`. Timestamps are Unix milliseconds. Backend `ProviderUsageService.read(force)` caches for 60 seconds and coalesces concurrent requests; `dispose()` aborts active default probes. Current backend failures return `unavailable` and no windows. The frontend also handles the error state.

## Sources and boundaries

- **Codex:** starts the installed CLI with `app-server`, initializes its private stdio protocol, and calls only `account/rateLimits/read`. It starts no model turn. It selects the `codex` bucket when present, otherwise the returned `rateLimits`; primary/secondary labels derive from the declared window duration. Probe timeout is 10 seconds and total stdout is capped at 1 MiB. Shutdown closes stdin, sends SIGTERM, and escalates after 500 milliseconds if necessary.
- **Claude:** reads the existing macOS `Claude Code-credentials` Keychain item, using the config-directory hash suffix when `CLAUDE_CONFIG_DIR` is set. Keychain reads time out after 3 seconds; denial returns unavailable. On non-macOS systems it can read the existing `.credentials.json` in the Claude config directory, bounded to 64 KiB. This reader does not establish Windows/Linux app packaging support. No credential is refreshed, repaired, rewritten, or returned to the UI.
- Claude sends a read-only GET to the fixed `https://api.anthropic.com/api/oauth/usage` endpoint with the existing bearer token and OAuth beta header. Redirects are rejected. The whole probe is bounded by a 10-second abort signal and the response by 64 KiB. This endpoint is a compatibility integration, not a stable public API guarantee; provider changes or unsupported accounts can make usage unavailable.
- Claude maps real `five_hour` and `seven_day` windows. Optional `weekly_scoped` entries in `limits` use their model display name and percentage, including Fable. These take precedence over Fable aliases `fable_weekly`, `fable_seven_day`, then `seven_day_fable`. Bare `fable` is ignored because its window duration is ambiguous. Percentages must be finite and within 0–100. Missing fields stay missing; reset timestamps accept ISO strings or epoch seconds, including fractional seconds, and become integer milliseconds.

Raw credentials, provider bodies, and CLI diagnostics are not exposed in returned errors. Usage does not estimate subscription quota from token counts or dollar costs. `BUDDY_NATIVE_TEST_MODE=1` uses the parent IPC's deterministic fixture results instead of real account probes. Unit tests inject credential/fetch/CLI fixtures; no paid model calls are required. Account access and provider compatibility remain separate from test success.

## References

- [OpenAI Codex App Server](https://learn.chatgpt.com/docs/app-server): initialization and account rate-limit RPC.
- [Claude Code status line](https://code.claude.com/docs/en/statusline): official structured usage and reset fields.
- Orca audit at `ba5f708290b72012132fb23a6f016b8fd5601718`: [OAuth request](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/rate-limits/claude-oauth-usage-request.ts), [credential lookup](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/claude-accounts/keychain.ts), [Fable fixtures](https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/main/rate-limits/claude-fetcher-fable-usage.test.ts). Buddy's adapter was written locally; these references informed the compatibility contract.
