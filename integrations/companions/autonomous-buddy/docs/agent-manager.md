# Autonomous Buddy — Desktop agent manager

The optional `desktop/` app provides local project, worktree and agent-session management. Electron's main process owns agent processes, Git/file access and persistence. React renders the workspace. The existing `macos/` Swift app continues to own pairing, device WebSocket and native computer-use executors; this slice adds no connection between the two apps.

The layout is inspired by Orca's basic workspace. This implementation is written locally from scratch; no Orca source or assets were copied. Direct reuse later requires a separate source/dependency license audit.

## Workspace

- Left: project folders, Git worktrees and their sessions, search, unread markers and a Needs attention view. Open project uses the native directory picker. A new worktree creates a new branch and a sibling directory named from the project, branch and a random suffix; it does not move existing work.
- Center: session tabs, rename, status, streamed transcript, prompt/follow-up composer and Stop. Terminal sessions use xterm with a real `node-pty` shell and resize with the pane. File and diff previews open here too.
- Right: working changes, unified diff on click, expandable local file tree and up to 30 recent commits. Git status refreshes every five seconds while visible and on focus/manual refresh. The recent-commit list is not a full branch/merge graph. File preview is read-only, limited to nonbinary files up to 1 MiB. Root-file filtering is not full-project search.
- Pane widths can be dragged; the sidebar can be hidden. Cmd/Ctrl+K focuses search, Cmd/Ctrl+N opens a new session, Enter sends and Shift+Enter adds a newline.

Ordinary directories can be projects without Git. Git worktree creation requires a repository. Removing a project only deletes its registration, saved sessions and transcripts; it leaves project files and worktrees on disk and is rejected while its sessions are running or starting.

## Model and local protocol

The source contract is [`desktop/src/shared/types.ts`](../desktop/src/shared/types.ts).

| Record | Fields / role |
| --- | --- |
| `Project` | `id`, `name`, canonical local `path` |
| `Worktree` | `path`, `branch`, `head`, `primary`, optional `locked`; read from Git |
| `Session` | `id`, `projectId`, `worktreePath`, `title`, `provider`, optional `providerSessionId`, `status`, `createdAt`, `updatedAt`, `unread` |
| `SessionEvent` | `id`, `sessionId`, monotonically increasing session-local `seq`, `at`, `type`, `text` |

Providers are `codex`, `claude`, `terminal`. Event types are `prompt`, `output`, `status`, `error`, `result`, `terminal`. Status values are `idle`, `running`, `needs_input`, `completed`, `error`, `stopped`. A running terminal displays **Shell active**; it is not inferred to be a coding agent.

The sandboxed renderer uses the finite `window.buddy` preload API. Request/reply uses `ipcRenderer.invoke('buddy:<method>')`; `buddy:update` pushes either a whole projects/sessions/providers snapshot or one session event. `session(id)` retrieves retained history, merged with live events by sequence number. There is no HTTP/WebSocket listener, remote cursor API, turn ID or request-id deduplication in this slice. Concurrent sends to one session are rejected; different sessions can run in parallel.

IPC handlers verify the sender window/main frame and local renderer URL. The renderer has no Node integration. Session workspaces must be registered project worktrees; file browsing rejects path traversal and symlinks that leave the selected workspace. These boundaries constrain the UI API, not the capabilities of commands the user runs in a shell or agent CLI.

## Agent lifecycle

Codex uses `codex exec` JSON events with `sandbox_mode="workspace-write"` and `--skip-git-repo-check` for explicitly selected non-Git project folders; follow-ups use `exec resume` with the saved thread ID. Claude Code uses print mode with stream JSON and partial messages; follow-ups pass `--resume` with its saved session ID. Prompts go through stdin, without a command shell. The CLIs use their own installed configuration and accounts.

Sending starts a new CLI process for that turn, preserving the provider conversation ID across turns. Output arrives asynchronously after the send request starts the process. Completion requires a recognized completion event and successful exit. Unknown plain text and stderr diagnostics are visible but do not imply completion. A provider that returns a different ID is rejected; if an earlier turn never yielded an ID, follow-up is refused rather than silently starting a different conversation.

`needs_input` currently means Claude reported permission denials in its result. It is not a general detector for questions, and there is no live permission-approval bridge. Review the result and send a follow-up, or use a separate terminal for interactive CLI workflows; that terminal does not automatically attach to the managed agent conversation. Codex's noninteractive execution also has no approval dialog supplied by Buddy.

Completed, needs-input and error transitions mark a session unread. Opening it marks it read. Desktop notifications are requested on those transitions while the window is unfocused, subject to OS support/permissions. No notification reaches the lamp yet.

Stop signals the process group on POSIX and escalates while still live. Closing the last window quits the app and stops all managed sessions. Background operation after app closure is not supported.

## Persistence and restore

Electron `app.getPath('userData')/manager.json` stores schema version 1, project registrations, sessions and bounded event history. On macOS the normal path is `~/Library/Application Support/Autonomous Buddy/manager.json`; `BUDDY_DATA_DIR` overrides the directory. Writes replace the file atomically. Output saves are debounced by 150 ms, so an abrupt crash can lose the latest buffered events.

Each session retains at most 2,000 events and 2 MiB of text (measured as JavaScript string length); each event is capped at 65,536 characters. Sequence numbers continue even when older events are removed. This is recent history, not an archival transcript.

On startup, saved `running`/`needs_input` sessions become `stopped`. Nothing restarts automatically. Agent follow-up can resume when a provider ID was saved and the provider still has that conversation. Terminal history is replayed for display; a new terminal session is required for a new shell. Corrupt or unsupported stored state produces an error rather than silently discarding saved work.

## Development and limits

Use the commands and prerequisites in [`desktop/README.md`](../desktop/README.md): `npm install`, `npm run lint`, `npm test`, `npm run build`, `npm run test:e2e`, then `npm start`. Backend tests use temporary repositories and simulated agent launchers. Electron smoke tests use temporary state and simulated agent CLI responses; no paid model call is needed. Build/test success does not replace testing installed/authenticated CLI versions against live providers.

This slice has no OpenCode/custom provider adapter, Swift IPC, lamp voice routing, remote companion, full Git commit graph, staging/commit/push UI, rich research-artifact renderer or packaged signed release. Electron/React enables later platform work; Windows/Linux execution and packaging are not established by macOS validation.

The next integration should carry explicit project/session IDs from lamp voice routing through an authenticated local Swift-to-manager boundary, then send status/events back. Session management should remain independent of screenshot/click/type executors.
