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

Each session tab has a visible **×**. Closing a tab hides that tab; it does not stop an agent, terminate a terminal, or delete session history. Closing the active tab chooses an adjacent open tab, or leaves the worktree empty. Select the session in the sidebar to reopen it. Double-click a tab to rename its session.

**⌘W** closes the active file preview first, otherwise the active session tab. It does not close a background session while a dialog or menu is open. Closed tab IDs and current selection persist in local UI preferences across app restarts. Process survival across app restarts is a separate concern: closing the whole app still shuts down its owned agent processes.

## Orca audit and remaining differences

The implementation is original code informed by the upstream [WorktreeContextMenuView](https://github.com/stablyai/orca/blob/1a8640adb6e86abb342a8025892300b2835f3e8e/src/renderer/src/components/sidebar/WorktreeContextMenuView.tsx), [context-menu commands](https://github.com/stablyai/orca/blob/1a8640adb6e86abb342a8025892300b2835f3e8e/src/renderer/src/components/sidebar/use-worktree-context-menu-commands.ts), and [TabBar](https://github.com/stablyai/orca/blob/1a8640adb6e86abb342a8025892300b2835f3e8e/src/renderer/src/components/tab-bar/TabBar.tsx). In that audited revision, Orca's Update menu invokes rename, and New group from project creates a project group before assigning the project; neither means pulling Git changes or creating a worktree.

This is not full Orca parity. Buddy currently uses a fixed set of organizational statuses and a single scrollable context menu instead of flyout submenus. Multi-select operations, drag reorder, split panes, browser/editor tabs, project-group administration independent of its projects, remote/mobile access, and automatic process resume after Sleep are not implemented here.

Validation: desktop `npm run lint` and `npm run build`; packaged Electron smoke coverage is maintained in `desktop/tests/electron-smoke.mjs`. See [agent-manager.md](agent-manager.md) for the native boundary and provider/session lifecycle.

## Provider usage footer

The bottom usage bar shows Claude and Codex quota windows returned by `providerUsage(refresh?)`: the window label, percentage used, a small meter, and time until reset when supplied. Hover or keyboard-focus a provider to inspect exact reset timestamps and availability details. Missing or failed quota data displays Sign in or Not available; the UI never replaces unknown quota with zero percent. It requests data on mount and every 60 seconds without overlapping requests, with a manual Refresh agent usage button. Provider authentication and quota retrieval stay in the main process; credential values never enter this renderer API.
