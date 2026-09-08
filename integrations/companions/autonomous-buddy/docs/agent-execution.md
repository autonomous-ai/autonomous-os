# Agent execution mode

Managed Codex and Claude sessions run with full access and no CLI approval prompts,
as explicitly requested for this Buddy workflow. This applies to both a new
conversation and follow-ups using the saved provider session ID:

- Codex uses `codex exec --dangerously-bypass-approvals-and-sandbox`; follow-ups
  include `resume` and the saved thread ID. JSON output and stdin prompts remain.
- Claude uses `claude --print --dangerously-skip-permissions`; follow-ups include
  `--resume` and the saved session ID. Stream JSON and partial messages remain.

The session view displays **Full access · no approvals**. Flags apply only to CLI
processes launched by Buddy; no global CLI configuration is rewritten. Agent
file/tool operations inherit the user's filesystem permissions. Native macOS
Accessibility, Screen Recording, TCC and Buddy pause/pairing remain separate
controls and are not bypassed by these flags. A provider/account/OS failure can
still stop a turn; the flags do not guarantee every operation succeeds.

The invocation regression test checks both providers with and without resume IDs.
Installed CLI help was checked for the supported flags. This validation does not
claim a paid live-model task has been executed.

## Authenticated validation

From `desktop/`, explicitly run `BUDDY_LIVE_PROVIDER_TEST=1 node tests/provider-live.mjs` to validate installed Codex and Claude against their real accounts. This opt-in check creates a temporary empty project and two no-tool turns per provider, verifies completion and recalled context using the exact saved provider session ID, then removes its temporary Buddy state. Provider-side conversation records may remain. It uses real model calls and is not part of unit or mocked Electron tests.

On 2026-09-08 both providers passed initial completion and exact-session follow-up on this Mac with the no-approval flags above.
