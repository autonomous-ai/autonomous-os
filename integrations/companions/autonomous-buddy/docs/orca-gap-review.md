# Orca source review and Buddy workflow gaps

Reviewed on 2026-09-08 against the full local checkout of `stablyai/orca`, commit
`ba5f708290b72012132fb23a6f016b8fd5601718` (package version 1.4.197). This differs
from the earlier cached tree `1a8640adb6e86abb342a8025892300b2835f3e8e`; all Orca
links below pin the commit actually inspected. This is a static source review,
not a claim that Orca was built/run or that Buddy reaches parity. No Orca code or
assets were copied. Buddy findings reflect the current working tree, including
group/workspace/quota/tab-close, native relay and the new split/Git review work.
Split/Git source has been inspected; its new combined E2E is pending at this
update. Passing baseline bundled E2E is not evidence that those new flows passed.

## Main finding

Orca's useful reference is its ownership model and everyday workflow, not just
three columns. A repository owns workspaces; a workspace owns tab groups; tabs
own terminal leaves or other content; each leaf binds its PTY and provider session.
Status, attention, resume and keyboard focus follow those identities. Buddy has
working local projects, sessions and native integration, but still lacks several
of these everyday interactions. Coloring cards or adding isolated buttons does
not close those gaps.

## Source-backed comparison

“Present” means a concrete Buddy implementation exists, not full behavioral parity.
“Partial” names the boundary already supported and what remains. “Missing” means
no matching path was found in the inspected Buddy contract/components.

| Workflow | Orca evidence and behavior | Buddy evidence and current gap | Priority |
| --- | --- | --- | --- |
| Project and repository organization | [Repo][repo] has display identity, project group/order, base-ref/path and execution owner; [group actions][groups] fence asynchronous catalogs by host. | **Partial.** `desktop/src/shared/types.ts` and `renderer/WorkspaceSidebar.tsx` have canonical project paths and string groups. No independent group IDs/order, per-repo setup policy or host ownership model. Local ownership is sufficient for current MVP; do not import host complexity yet. | Core: predictable local ordering/group operations. Remote ownership later. |
| Worktree lifecycle and status hierarchy | [Worktree][workspace] separates workspace status definitions, pinned/unread/archived metadata and Git/host identity; [board grouping][kanban] applies status and manual/recency order. | **Partial.** `shared/types.ts`, `main/manager.ts` and `renderer/WorkspaceSidebar.tsx` support name, parent relation, pin/unread and fixed active/review/done. Create/delete/sleep exist (`manager.ts`). Custom statuses, archive model, manual ordering and board are absent. | Core: clear hierarchy/status independent from agent process state. Board/custom statuses optional. |
| Worktree creation policy | [Repo][repo] stores base ref/path, hooks and shared/symlink paths, rather than treating each checkout as an anonymous folder. | **Partial.** Buddy creates a branch/worktree with a sibling path and validates existing worktrees. No UI for base ref, setup command, dependency reuse or failed-setup state. | Core after layout/session reliability: let users choose base branch and expose setup failures. |
| Tabs and content ownership | [Tab model][layout] has recursive groups, explicit group/worktree/entity IDs, preview versus durable tab and content types. | **Partial.** `renderer/App.tsx` persist closed-session tabs, reopen from sidebar, rename and close via Cmd+W without stopping. File/diff preview is a single transient surface; per-tab pane layouts now persist, but no tab order/groups, pinning or moving content between groups. | Core: stable open/close/reopen/active tab and preview semantics before cosmetic changes. |
| Parallel terminal panes | [Terminal layout][terminal] stores stable leaf IDs, focused/expanded leaf and per-leaf buffers; [split actions][split] implement split right/down with inherited live cwd. | **Partial.** `desktop/src/renderer/SessionWorkspace.tsx` and `split-layout.ts` now implement a persisted recursive keyed split tree, right/down creation of a real Terminal N, independent focus, resize and pane close without stopping the session. Splits start at the worktree root, not the source shell’s live cwd. Arbitrary existing-agent placement, cross-tab pane moves and focus-navigation shortcuts remain absent. | Core: validate input isolation and restore; then existing-agent placement/live cwd. |
| Start, follow-up, stop and sleep/resume | [Sleeping session record][resume] binds provider ID, transcript path, launch environment and restore origin; [hibernation][hibernate] refuses to kill before capturing a resumable record. | **Partial.** `main/manager.ts` and `main/providers.ts` run/resume Codex/Claude and stop process groups. Sleep stops sessions; restart marks running sessions stopped and requires explicit follow-up. Terminal scrollback is bounded history, not live process restoration. | Core: make sleep/wake/restore semantics visible; preserve conversation before claiming resume. |
| Agent state and interactive input | [Status contract][status] explicitly distinguishes working/blocked/waiting/done from terminal titles; [approval card][approval] sends the chosen literal option to its PTY; [chat subscriptions][chat] bind live transcript frames to subscription/session IDs. | **Partial.** Buddy parses structured completion/ID events and Claude permission-denial result into needs_input (`main/providers.ts`). It does not expose live question/approval decisions or provider tool/message structure. Current user policy explicitly requests no-approval execution; [Orca permission flags][permissions] also model this mode. | Core: reliable waiting/error/completion and follow-up context. Approval UI is optional under the chosen no-approval policy, not a reason to re-enable prompts. |
| Review changes and finish work | [Bulk actions][stage] stage/unstage selected paths; [commit area][commit] represents commit/stage/push actions; [combined diff actions][diff] distinguish branch/commit/working sections and allow opening/editing files. | **Partial.** `desktop/src/renderer/GitPanel.tsx` and `main/git.ts` now stage/unstage individual files and commit all current index entries with an explicit message. Working files remain on disk. Unified previews remain read-only; combined/branch diff, hunk actions, edit/save and push are absent. Existing staged changes are included, not just files staged through Buddy. | Core: validate precise index effects; combined review next. Push remains explicit separate scope. |
| Commit history and research/file results | [History actions][history] expand commit files and open single/combined commit diffs; [tabs][layout] distinguish editor/diff/browser and session surfaces. | **Partial.** `desktop/src/renderer/GitPanel.tsx` now expands a selected commit into changed files and opens each file diff against its first parent (empty tree for root commits). No full graph, combined commit review or structured research artifact viewer/editor. | Basic inspection implemented; validate rename/root-commit behavior. Rich artifact/browser tools later. |
| Unread and attention | [Auto-ack][ack] acknowledges the exact active terminal leaf and state timestamp; viewing one split leaf must not clear sibling attention. | **Partial.** `desktop/src/renderer/SessionView.tsx` marks a session read only when its pane and document are focused. `main/index.ts` notifications reopen/focus their session, and the relay sends notices to lamp. Workspace unread remains separate metadata; no general live blocked detector or Orca state-timestamp/retained-leaf acknowledgement model. | Pane-aware acknowledgement and session navigation implemented; verify concurrent completion behavior. |
| Usage visibility | [Usage state][usage] has opt-in scan state, summaries, daily/model/project breakdowns and recent sessions for multiple providers. | **Partial.** `main/provider-usage.ts`, `renderer/ProviderUsageBar.tsx` show subscription quota windows with ready/unavailable/error. This is useful but not token/cost analytics or project attribution. | Keep quota accurate now; analytics are optional. |
| Settings, keyboard and navigation | [Key registry][keys] defines file/worktree/settings navigation; [pane shortcuts][panekeys] define split actions. [Workflow settings][settings] share searchable metadata across Git, terminal and other sections. | **Partial.** `renderer/App.tsx` provide search/new-session/close shortcuts and simple settings; no command palette registry, configurable bindings, quick-open file picker, MRU tabs or focus-next-pane. | Core: discoverable shortcuts for switching workspace/tab/pane; broad settings customization later. |
| Mobile/remote and product services | [Repo ownership][repo], [catalog actions][groups], and [package][package] reveal runtime/SSH ownership and many optional services. | **Missing by design.** Buddy's paired lamp controls the local manager through Swift; it is not Orca's remote multi-host/mobile session runtime. | Optional after the local daily workflow is complete. |
| Native computer use | Orca includes broader browser/runtime integrations in [package][package]; those are not a prerequisite for the workflow models above. | **Present as a separate internal boundary.** Swift pairing/permissions/commands/menu plus Electron manager are one installed app; [native bridge](./native-bridge.md) documents actual relay/lifecycle. This does not yet establish an autonomous screenshot planning loop. | Preserve current Swift executor; do not replace it to copy the UI. |

## Reuse boundary and licensing

The [root license][license] is MIT with `Copyright (c) 2026 Lovecast Inc.`.
Copying an Orca source module requires that notice and the full MIT text in the
redistribution. This review did not audit every dependency or authorize copying
all bundled assets. [Site notices][notices] separately identify Geist under OFL;
a root MIT license is not permission to strip font/dependency notices. The checkout
also contains a separately licensed mobile audio package and vendored grammar
license. Inspect every selected file and its transitive imports before reuse.

Good *extraction candidates*, not drop-in modules: immutable tab/layout trees,
stable pane identifiers, ordering/status helpers, keybinding definitions and
associated regression cases. Even `tab-types.ts` imports Orca-specific agent,
execution-host and AI-Vault types; extract a small Buddy-owned contract instead of
copying its whole type graph. No candidate is marked “directly reusable as is”
without that dependency audit.

Renderer source-control and pane components are coupled to `useAppStore` Zustand
slices, `@/runtime/*` routing, connection ownership, translation, telemetry,
Monaco and Orca preload contracts. The [dependency manifest][package] adds
node-pty/xterm plus SSH, filesystem watchers, Claude SDK, browser automation,
speech models and service clients; its build scripts also build CLI/runtime
components. Reusing a complete pane would import much more than React markup.
Do not clone the whole application into Buddy or adopt its remote runtime merely
to reproduce local tabs.

Keep three independently testable boundaries: (1) local manager with durable
project/workspace/session/pane identity; (2) React views consuming an explicit
finite manager API; (3) OS-native computer executor and device transport. Later
Windows/Linux helpers replace boundary 3, not the session data model.

## Implementation order and acceptance evidence

1. **Fix the workflow foundation as one coherent slice:** durable tab/content and
   pane model, split/focus/close/reopen, correct cwd and stable session binding.
   Acceptance: two agents and a shell in one workspace, independent streaming,
   input never crosses panes, close does not kill sibling work, reopen/restart
   preserves identity and accurately reports what stopped.
2. **Finish the task loop:** reliable running/waiting/completed/error, explicit
   follow-up on the same provider session, stop/sleep/wake semantics and precise
   unread/notification navigation. Respect the requested no-approval policy.
   Acceptance: concurrent turns, cancellation and restart/reconnect tests plus
   installed authenticated CLI checks; mock green tests alone are insufficient.
3. **Finish local review:** changed-file selection, combined diff, stage/unstage,
   commit draft/result and selected-commit file/diff inspection. Acceptance:
   temporary repositories with staged/unstaged/untracked/renamed files; only
   selected paths change, errors stay visible, no automatic push.
4. **Polish from the shared action model:** keyboard palette/navigation, persisted
   local ordering and focused setup/settings; quota truthfulness remains required.
   Defer custom boards, remote/mobile runtime, analytics and rich artifacts until
   they serve a validated daily workflow.

The order above is the original roadmap. Basic splits, stage/commit and commit
inspection now have implementations as recorded in the updated matrix. Next
work is validation of these flows and remaining gaps, not reimplementation.
These priorities do not prove the new split/Git integration E2E passed. No production code changed for this audit. Validation consisted of
reading the pinned source and comparing current Buddy contracts/components;
Orca dependencies were not installed and Orca was not built or executed.

[repo]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/repo-types.ts#L42
[workspace]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/worktree/types.ts#L52
[groups]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/store/project-groups/project-group-catalog-actions.ts#L16
[kanban]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/components/sidebar/workspace-kanban-worktree-groups.ts#L22
[layout]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/tab-types.ts#L8
[terminal]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/terminal-tab-types.ts#L60
[split]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/components/terminal-pane/use-terminal-pane-split-actions.ts#L39
[resume]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/agent-session-resume.ts#L45
[hibernate]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/store/terminals/terminal-pane-hibernation.ts#L47
[status]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/agent-status-types.ts#L25
[approval]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/components/native-chat/NativeChatApprovalCard.tsx#L5
[chat]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/preload/api/native-chat-api.ts#L16
[ack]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/hooks/useAutoAckViewedAgent.ts#L23
[stage]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/components/right-sidebar/source-control/commit/use-bulk-actions.ts#L26
[commit]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/components/right-sidebar/source-control/commit/commit-area.tsx#L30
[history]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/components/right-sidebar/source-control/sync/use-git-history-commit-actions.ts#L41
[diff]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/components/editor/combined-diff/review-controls/use-combined-diff-section-actions.ts#L27
[keys]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/keybindings/definitions-core-1.ts#L6
[panekeys]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/keybindings/definitions-core-4.ts#L22
[usage]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/store/slices/usage-provider-slices.ts#L19
[settings]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/renderer/src/hooks/settings-navigation-workflow-sections.ts#L28
[permissions]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/src/shared/tui-agent-permissions.ts#L7
[license]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/LICENSE#L1
[notices]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/docs/site/THIRD_PARTY_NOTICES.md#L3
[package]: https://github.com/stablyai/orca/blob/ba5f708290b72012132fb23a6f016b8fd5601718/package.json#L1
