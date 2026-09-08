# Agent execution mode

Managed Codex and Claude sessions run with full access and no CLI approval prompts,
as explicitly requested for this Buddy workflow. This applies to both a new
conversation and follow-ups using the saved provider session ID:

- Interactive Codex uses `codex --dangerously-bypass-approvals-and-sandbox --no-alt-screen`; resume supplies the exact saved thread ID.
- Interactive Claude uses `claude --dangerously-skip-permissions` with `--session-id` for a new conversation or `--resume` for an existing one.
- Retained or explicitly selected structured sessions use `codex exec` JSON/stdin or Claude `--print` stream JSON with the same full-access flags. Only this legacy structured view displays **Full access · no approvals**.

Interactive sessions show the provider's actual terminal UI; see [interactive sessions](interactive-sessions.md). Claude can still show its own first-use trust screen for a new directory; Buddy does not bypass that CLI-controlled screen. This differs from per-tool approvals disabled by the execution flags. Flags apply only to CLI processes launched by Buddy; no global CLI configuration is rewritten. Agent
file/tool operations inherit the user's filesystem permissions. Native macOS
Accessibility, Screen Recording, TCC and Buddy pause/pairing remain separate
controls and are not bypassed by these flags. A provider/account/OS failure can
still stop a turn; the flags do not guarantee every operation succeeds.

The invocation regression test checks both providers with and without resume IDs.
Installed CLI help was checked for the supported flags. Mock tests establish invocation contracts; real-provider validation is reported separately below.

## Authenticated validation

From `desktop/`, explicitly run `BUDDY_LIVE_PROVIDER_TEST=1 node tests/provider-live.mjs` to validate installed Codex and Claude against their real accounts. This opt-in check creates a temporary empty project and two no-tool turns per provider, verifies completion and recalled context using the exact saved provider session ID, then removes its temporary Buddy state. Provider-side conversation records may remain. It uses real model calls and is not part of unit or mocked Electron tests.

On 2026-09-08 both providers passed initial completion and exact-session follow-up on this Mac with the no-approval flags above.

Interactive Codex and Claude initial sessions and exact-ID resumes were also verified with real provider hooks on this Mac. Hook readiness/status and process liveness are tracked separately; CLI behavior remains version dependent.
