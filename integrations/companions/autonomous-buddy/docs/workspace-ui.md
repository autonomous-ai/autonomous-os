# Agent workspace UI

Autonomous Buddy ships the agent workspace and native computer controls in one app. The project sidebar now groups each worktree's agent sessions and terminals, with counts, running/attention indicators, unread markers, and an expandable session list. Search matches project names, branch names, and session titles. The Computer & device panel remains available from the same sidebar.

## Workspace actions

Right-click a worktree card or use its accessible **…** button. The menu supports keyboard navigation with Arrow Up/Down, Home/End, and Escape.

- **Update workspace name** changes its display name, preserving its Git branch and path.
- **New session** starts the existing provider chooser in that worktree. **New worktree** creates a real Git worktree through the existing branch dialog.
- **Move to status** assigns Active, In review, or Done. These are organizational states; changing them does not claim an agent completed its task.
- **Open in** opens the validated workspace in Finder, Terminal, or Visual Studio Code through finite main-process handlers. A missing application reports an error.
- **Copy path**, **Pin/Unpin**, and **Mark read/unread** act on the selected worktree. Mark read also clears unread session markers. Pinned siblings sort before other siblings.
- **New group from project** creates a named sidebar group and assigns the whole project to it. **Move project to group** selects an existing group or No group. These actions do not move files.
- **Set parent worktree** organizes worktrees in the same project. Children appear below their parent with indentation and a parent label. Selecting No parent removes the relationship. The manager rejects cycles. No rebase or branch mutation occurs.
- **Sleep workspace** confirms before stopping running agents and terminals. Sessions and history remain. It does not promise to resume an interrupted process automatically.
- **Delete worktree** confirms before removing the folder. Primary, locked, dirty, or active worktrees cannot be removed; Git removal never uses force. The branch and session history remain.

Session rows have their own **…** menu for rename, stop, and explicit deletion. Deleting a stopped session requires confirmation and deletes its conversation history, while keeping project files.

Workspace metadata is persisted by the manager as `projectId`, `worktreePath`, `pinned`, `status`, `unread`, optional `displayName`, and optional `parentWorktreePath`. Project groups use the optional project `group` field. Renderer actions use the allowlisted `updateWorkspace`, `setProjectGroup`, `sleepWorkspace`, `removeWorktree`, `openWorkspace`, `copyWorkspacePath`, and `removeSession` IPC APIs; filesystem/process validation stays in the main process.

## Tabs

Each session tab has a visible **×**. Closing a tab stops its session process and archives the session from the active sidebar; retained history is not deleted. Closing the active tab chooses an adjacent open tab, or leaves the worktree empty. Double-click a tab to rename its session. Use **Stop** instead when you want to keep an interactive session visible and resume it later.

**⌘W** closes the active file preview first, otherwise the active session tab with the same stop/archive behavior. It does not close a background session while a dialog or menu is open. Closing the macOS workspace window keeps the app, native helper, and other sessions running; explicit Quit stops them. See [interactive sessions](interactive-sessions.md).

## Orca audit and remaining differences

The implementation is original code informed by the upstream [WorktreeContextMenuView](https://github.com/stablyai/orca/blob/1a8640adb6e86abb342a8025892300b2835f3e8e/src/renderer/src/components/sidebar/WorktreeContextMenuView.tsx), [context-menu commands](https://github.com/stablyai/orca/blob/1a8640adb6e86abb342a8025892300b2835f3e8e/src/renderer/src/components/sidebar/use-worktree-context-menu-commands.ts), and [TabBar](https://github.com/stablyai/orca/blob/1a8640adb6e86abb342a8025892300b2835f3e8e/src/renderer/src/components/tab-bar/TabBar.tsx). In that audited revision, Orca's Update menu invokes rename, and New group from project creates a project group before assigning the project; neither means pulling Git changes or creating a worktree.

This is not full Orca parity. Buddy currently uses a fixed set of organizational statuses and a single scrollable context menu instead of flyout submenus. Multi-select operations, drag reorder, browser/editor tabs, project-group administration independent of its projects, remote/mobile access, and automatic process resume after Sleep are not implemented here.

Validation: desktop `npm run lint` and `npm run build`; packaged Electron smoke coverage is maintained in `desktop/tests/electron-smoke.mjs`. See [agent-manager.md](agent-manager.md) for the native boundary and provider/session lifecycle.

## Provider usage footer

The bottom usage bar shows Claude and Codex quota windows returned by `providerUsage(refresh?)`: the window label, percentage used, a small meter, and time until reset when supplied. Hover or keyboard-focus a provider to inspect exact reset timestamps and availability details. Missing or failed quota data displays Sign in or Not available; the UI never replaces unknown quota with zero percent. It requests data on mount and every 60 seconds without overlapping requests, with a manual Refresh agent usage button. Provider authentication and quota retrieval stay in the main process; credential values never enter this renderer API.


## Split session panes

Use **Split right** or **Split down** in a pane header to create a real terminal session in the same project and worktree. Splits can nest in both directions; this is a recursive layout, not a fixed two-pane view. Each leaf renders its own session output and controls. Click or focus a pane to select it; only the focused pane automatically marks its session updates read.

Drag a divider to resize its two children, or focus the divider and use its direction's arrow keys (Home/End select the bounds). Ratios stay between 15% and 85% so both sides remain reachable. Closing a pane stops and archives that pane's session, then collapses an empty split. Other split sessions keep running. Closing the original session or the last pane closes that tab; remaining sessions can be opened from the sidebar as their own tabs.

The layout is saved per original session tab in local UI preferences, including directions, split ratios, and session IDs. Restore and live snapshot updates remove unknown, archived, deleted, duplicate, or foreign-worktree sessions; focus moves to the first surviving pane when needed. Restoring layout does not restart stopped terminal processes. `split-layout.test.ts` covers nested layouts, closing and collapsing, identity validation, and resize persistence; the Electron smoke test verifies real PTY behavior separately.

Legacy structured-agent prompt drafts are saved per session as `buddy.draft.<sessionId>` in local UI preferences, so changing tabs or splitting a pane keeps unsent text. A successful send, session close, or explicit session deletion clears that draft. Interactive CLI input belongs to the CLI and does not use this draft store. Terminal hydration respects the focused pane and changing focus does not rebuild its terminal view. Unread updates are cleared automatically only for the focused pane while the app window has focus; returning to the window marks that pane read. Clicking a desktop session notification selects its exact session, including when the window is still loading.

On the first workspace load after upgrading, saved tab-close intents from the old hide-only UI are archived through the same close API. Already archived sessions keep their stored history.

Project headers can collapse/expand their worktrees and nested sessions by mouse or keyboard. The choice is saved locally per project and survives restart; it does not close sessions or change the selected voice target. Search temporarily reveals matching worktrees, then restores the saved collapse state when cleared. The separate + button still creates a worktree.

The sidebar uses the Projects header + button to open/register a project; the duplicate bottom Open project button is removed. The empty-workspace onboarding action remains available.

Create worktree now includes a Codex / Claude Code / Terminal selector; unavailable providers are disabled. Creation opens a session for the chosen provider in that new worktree and selects it. If session startup fails, the created worktree is retained and the error is reported; the dialog closes so retrying cannot accidentally recreate the branch.

The worktree agent picker uses a themed menu with the same provider symbols as session tabs (Codex sparkle, Claude asterisk, Terminal prompt). The selected value and each option show their symbol; session creation cards share these symbols. Tab navigates options, Enter selects, and Escape dismisses the menu without closing the dialog.
