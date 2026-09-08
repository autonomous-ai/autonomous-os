# Autonomous Buddy — Desktop agent manager

Autonomous Buddy is one desktop application with two internal components. Electron's main process owns agent processes, Git/file access, persistence and the visible React workspace. The embedded `macos/` Swift helper owns pairing, the device WebSocket and native computer-use executors. Users install and open one `Autonomous Buddy.app`; the helper is a child process, not a second application to install.

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

The sandboxed renderer uses the finite `window.buddy` preload API. Request/reply uses `ipcRenderer.invoke('buddy:<method>')`; `buddy:update` pushes either a whole projects/sessions/providers snapshot or one session event. `session(id)` retrieves retained history, merged with live events by sequence number. The app opens no local HTTP/WebSocket listener. Paired-device `agent.*` commands use the existing Swift WebSocket and private pipes: create/send have persistent request-ID receipts, and `agent.session` supports a bounded sequence cursor. See [native bridge](./native-bridge.md) for the exact contract. Concurrent sends to one session are rejected; different sessions can run in parallel.

IPC handlers verify the sender window/main frame and local renderer URL. The renderer has no Node integration. Session workspaces must be registered project worktrees; file browsing rejects path traversal and symlinks that leave the selected workspace. These boundaries constrain the UI API, not the capabilities of commands the user runs in a shell or agent CLI.

## Native computer-use integration

The main workspace's **Computer & device** panel shows helper readiness, paired device/connection, and Accessibility/Screen Recording grants. It offers Pair (with an optional host and discovered-device suggestions), Unpair, Pause/Resume when paired, Manage permissions, Activity, and Restart computer control if the helper is unavailable. Pair and Activity open the existing native windows; the bundled Swift helper keeps the native menu-bar icon (without a second Dock icon), with pairing, pause, Activity, Open Agent Manager and Quit Autonomous Buddy.

The Electron main process owns `NativeHelper`. JSONL over inherited stdin/stdout carries requests `{id, method, params}`, replies `{id, result}` or `{id, error}`, and pushed `{event: "state", state}` snapshots. Diagnostics are drained from stderr. There is no public socket or HTTP listener and no pairing token is returned to the renderer. Native methods are `status` (`ping` alias), `pair`, `unpair`, `pause`, `permissions`, `activity`, `command`, `agent_response`, `agent_event`, and `shutdown`. `command` routes the existing structured `{action, params, timeout_ms}` payload through the Swift dispatcher and audit path; it does not turn an agent session into a computer-use reasoning loop. `restart` is a manager-side lifecycle action, not a native protocol method. Renderer access remains behind the existing validated preload IPC boundary.

State includes pairing, connection/error, paused state, native permission flags, discovered devices and recent commands. Permission grants are checked, never inferred from bundling. Pause cancels active native work; Unpair follows the existing revoke-and-clear path. Electron closes stdin when quitting, and the Swift helper exits on EOF even if the parent crashes; explicit `shutdown` also terminates it. The manager escalates helper shutdown after two seconds if necessary. On macOS, closing the workspace window keeps device control and agent sessions running. The menu-bar Open Agent Manager action reopens the workspace; Quit Autonomous Buddy sends a private menu event to Electron to shut down the entire app and helper.

Native smoke tests set `BUDDY_NATIVE_TEST_MODE=1`: discovery and saved-pair reconnect are skipped, audit writes go to `/dev/null`, and pairing/unpairing, native UI and permission prompts are rejected. This flag does not disable every command executor, so tests must restrict dispatched commands to controlled checks. The tests do not establish live device connectivity or real agent-to-computer-use orchestration.

## Agent lifecycle

Codex uses `codex exec` JSON events with `--dangerously-bypass-approvals-and-sandbox` and `--skip-git-repo-check` for explicitly selected non-Git project folders; follow-ups use `exec resume` with the saved thread ID. Claude Code uses print mode with `--dangerously-skip-permissions`, stream JSON and partial messages; follow-ups pass `--resume` with its saved session ID. Prompts go through stdin, without a command shell. The CLIs use their own installed accounts. Both new turns and resumed turns run with full access and no CLI approval prompts, as explicitly requested; no global CLI settings or macOS permissions are changed. See [agent execution](./agent-execution.md).

Sending starts a new CLI process for that turn, preserving the provider conversation ID across turns. Output arrives asynchronously after the send request starts the process. Completion requires a recognized completion event and successful exit. Unknown plain text and stderr diagnostics are visible but do not imply completion. A provider that returns a different ID is rejected; if an earlier turn never yielded an ID, follow-up is refused rather than silently starting a different conversation.

`needs_input` currently means Claude reported permission denials in its result. It is not a general detector for questions, and there is no live permission-approval bridge. Review the result and send a follow-up, or use a separate terminal for interactive CLI workflows; that terminal does not automatically attach to the managed agent conversation. Codex's noninteractive execution also has no approval dialog supplied by Buddy.

Completed, needs-input and error transitions mark a session unread. Opening it marks it read. Desktop notifications are requested on those transitions while the window is unfocused, subject to OS support/permissions. Completion/attention notices also travel through the paired Swift WebSocket to the device. Reconnect republishes unread terminal states; delivery is best effort and notices may repeat.

Stop signals the process group on POSIX and escalates while still live. On macOS, closing the last window keeps the app and managed sessions running in the menu bar. Explicit Quit stops all managed sessions and the native helper; on other platforms, closing the last window also quits.

## Persistence and restore

Electron `app.getPath('userData')/manager.json` stores schema version 1, project registrations, sessions and bounded event history. On macOS the normal path is `~/Library/Application Support/Autonomous Buddy/manager.json`; `BUDDY_DATA_DIR` overrides the directory. Writes replace the file atomically. Output saves are debounced by 150 ms, so an abrupt crash can lose the latest buffered events.

Each session retains at most 2,000 events and 2 MiB of text (measured as JavaScript string length); each event is capped at 65,536 characters. Sequence numbers continue even when older events are removed. This is recent history, not an archival transcript.

On startup, saved `running`/`needs_input` sessions become `stopped`. Nothing restarts automatically. Agent follow-up can resume when a provider ID was saved and the provider still has that conversation. Terminal history is replayed for display; a new terminal session is required for a new shell. Corrupt or unsupported stored state produces an error rather than silently discarding saved work.

## Development and limits

Use the commands and prerequisites in [`desktop/README.md`](../desktop/README.md): `npm install`, `npm run lint`, `npm test`, `npm run build`, `npm run test:e2e`, then `npm start`. Backend tests use temporary repositories and simulated agent launchers. Electron smoke tests use temporary state and simulated agent CLI responses; no paid model call is needed. Build/test success does not replace testing installed/authenticated CLI versions against live providers.

Local macOS packaging: from `autonomous-buddy/` or `desktop/`, `make build` compiles Electron, rebuilds node-pty, builds Swift in release mode for the current architecture, and packages one verified app. The native executable is `Contents/Resources/native/AutonomousBuddy`, with SwiftPM resource bundles alongside it. `make install` rebuilds and installs `/Applications/Autonomous Buddy.app` via staging. Before replacement it checks bundle identities, signatures and the embedded executable; quits the exact previous app paths; and preserves prior bundles under `~/Library/Application Support/AutonomousBuddy/LegacyBackups/` with unique `.disabled` names. The old standalone `/Applications/AutonomousBuddy.app` is archived there too, so only one product remains in Applications. Replacement errors restore the old bundles. Pairing and manager state are not deleted. `make open` opens the installed app.

The bundle keeps `network.autonomous.ai.buddy.manager` for manager continuity, while the native pairing store remains unchanged. Electron launches the helper with `--embedded-helper` and communicates through private child-process pipes, without opening a local network listener. The helper is shut down with the parent; native functionality reports unavailable if helper startup fails. Native macOS permissions remain subject to OS checks and may require a fresh grant after repackaging. Device computer-use commands remain handled by Swift; `agent.*` requests are relayed to the manager with explicit project/session IDs.

Packaging uses ad-hoc signing unless `DEV_ID_APP` is supplied; the parent Makefile auto-detects a Developer ID when available. Build/install never notarizes. `app-signed`, `dmg`, `dmg-signed` and `notarize` operate on the unified product; the original native-only development/release recipes remain explicitly prefixed `native-*`. Developer ID distribution and notarization require separate release validation. Packaged startup supplements inherited PATH with login-shell PATH (5-second timeout) and common CLI directories. Set `BUDDY_APP_EXECUTABLE` to the packaged executable to run the Electron smoke suite against the bundle.

This slice has no OpenCode/custom provider adapter, remote companion, full Git commit graph, staging/commit/push UI, rich research-artifact renderer or packaged signed release. Electron/React enables later platform work; Windows/Linux execution and packaging are not established by macOS validation.

The lamp agent-management skill can list/create/send/read/stop managed sessions through paired-device commands. Stable request IDs protect create/send retries, while unfinished receipts after a crash refuse automatic replay. The mock WebSocket suite validates the relay without accessing a real device; live lamp/CLI behavior still needs end-to-end verification. Session management remains independent of screenshot/click/type executors. See [native bridge](./native-bridge.md) and the [Orca source gap review](./orca-gap-review.md).

### macOS app icon

The packaged app's Dock/Finder icon uses the same `lightbulb.fill` SF Symbol as
Buddy's connected menu bar indicator: a yellow bulb on a dark rounded square.
`desktop/scripts/generate-icon.swift` renders the iconset with AppKit during
packaging; `iconutil` produces the `.icns` supplied to Electron Packager. All
rendered icon files stay under ignored `desktop/artifacts/icon/`; no binary icon
assets are checked in.
