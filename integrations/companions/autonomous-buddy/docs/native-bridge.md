# Unified Buddy native bridge

Autonomous Buddy installs as one macOS app. Electron owns the project/session
manager, CLI processes and main window. Its bundled Swift executable owns the
menu bar, pairing, Bonjour discovery, device WebSocket, permissions and computer
executors. These remain separate internal responsibilities within one product.
The manager does not implement screenshot/click/type itself; Swift does not run
the coding model or choose the conversation context.

## Process and menu lifecycle

Electron starts `Contents/Resources/native/AutonomousBuddy --embedded-helper`
with inherited stdin/stdout pipes. There is no local listening socket or port.
Each pipe frame is one JSON object followed by a newline. Swift diagnostic lines
are ignored by the parent's JSON parser; stderr is drained separately. The
Electron renderer accesses a finite preload API guarded by the owning window,
main frame and local renderer URL.

The native menu remains visible in embedded production mode. It provides pairing,
pause, activity and an entry to open the manager. Permission controls are in
Computer settings. `open-manager` and
`quit` menu events are forwarded to Electron. Closing the manager window on macOS
keeps the app and helper alive. Quitting the app disposes manager processes and
closes helper stdin; EOF terminates Swift and disconnects the device. Electron
waits for helper exit, with a two-second kill fallback. An unexpected helper exit
is shown in Computer settings, where Restart creates a fresh helper.

State events contain pairing/connection state, device host, pause state,
permissions, discovery results and recent command summaries; they omit the pairing
token and buddy identifier. Normal native methods are `status`, `ping`, `pair`,
`unpair`, `pause`, `permissions`, `activity`, `command` and `shutdown`.

## Paired-device agent relay

The existing authenticated device WebSocket carries commands in this shape:

```json
{"id":"ws-command-id","action":"agent.send","params":{"project_id":"p1","session_id":"s1","request_id":"voice-turn-123","prompt":"Continue with tests"},"timeout_ms":10000}
```

Swift routes `agent.*` before the computer dispatcher. New agent commands are
rejected while Buddy is paused; standalone Swift without a manager returns an
explicit unavailable error. Pausing also cancels active computer execution, but
does not stop already accepted manager sessions. Use `agent.stop` after resuming,
or the desktop session controls, to stop an agent.

The two IPC directions are:

```json
{"event":"agent_request","id":"relay-uuid","command":{"id":"ws-command-id","action":"agent.list","params":{}}}
{"id":"ipc-request-uuid","method":"agent_response","params":{"id":"relay-uuid","response":{"id":"ws-command-id","ok":true,"result":{}}}}
```

Swift acknowledges the second frame with `result: {"accepted": true}` only when
the relay request is still pending and the original command ID and boolean `ok`
match. Unknown, expired, duplicate or mismatched responses are rejected. Errors
use `{ "id": "ws-command-id", "ok": false, "error": "message" }`.

The relay waits at most 10 seconds (a supplied timeout is clamped to 1–10000 ms).
Disconnect/reconnect cancels pending WebSocket tasks; each reply is bound to the
same source socket and cannot leak onto its replacement. This cancels waiting,
not effects already accepted by Electron. A timeout or disconnected response is
therefore an uncertain result, not proof that no session/task was created. Swift
never automatically replays the command.

## Desktop voice API

Projects must first be registered in Buddy. Routing uses explicit IDs, never the
currently selected UI tab. Follow-ups keep the same project/session IDs and use
the stored provider conversation ID. Voice-created sessions use the registered
project root; desktop-created worktree sessions can also be addressed by ID.

| Action | Parameters | Result |
| --- | --- | --- |
| `agent.list` | none | Current projects, sessions, workspaces and provider availability |
| `agent.create` | `project_id`, `provider` (`codex` or `claude`), `request_id`, optional `title` | Created session |
| `agent.send` | `project_id`, `session_id`, `request_id`, `prompt` | Acceptance with project/session IDs; output arrives separately |
| `agent.session` | `project_id`, `session_id`, optional `after_seq` | Session plus bounded events, `next_seq`, `truncated`, `has_more` |
| `agent.stop` | `project_id`, `session_id` | Current session after stop request; process exit may finish asynchronously |

Unknown actions, unknown projects and cross-project session IDs fail. A prompt
must be nonblank and at most 100000 JavaScript string units. `after_seq` must be a
nonnegative safe integer. Terminal input is not exposed through `agent.send`.

Create/send require a stable `request_id` of 8–128 letters, digits, underscores or
dashes. The manager fingerprints normalized action parameters and reserves a
receipt in `manager.json` before invoking the operation. Concurrent matching
requests share the pending result; finished receipts return their previous result
or error, including after restart. Reusing an ID with different parameters fails.
An unfinished receipt recovered after a crash is uncertain and refuses automatic
re-execution: inspect sessions before choosing a new ID. Storage uses atomic file
replacement, not a transactional commit across the filesystem and CLI process.
Receipts are capped at 10000 with no automatic eviction; new request IDs fail
when full, preserving previous deduplication records.

Session storage retains at most 2000 events and 2 Mi JavaScript string units of
text per session, with each event retaining at most its last 65536 string units.
`agent.session` returns at most 100 events and approximately 128 KiB of serialized
event data per page; each returned event text is capped at its first 8192 string
units. `truncated` signals a gap before the retained history, not text clipping.
Advance `after_seq` to `next_seq` while `has_more` is true. These are bounded
transcripts, not complete archival logs.

## Completion and attention events

Electron sends the native method `agent_event` with this parameter envelope:

```json
{"type":"agent_event","project_id":"p1","session_id":"s1","seq":17,"status":"needs_input","title":"Review tests","summary":"A decision is needed"}
```

Swift forwards it only to the current connected device and acknowledges
`result: {"sent": true|false}`. `sent` means queued for WebSocket write, not a
remote acknowledgement or confirmed spoken notification. The manager emits
`completed`, `needs_input` and `error` notices, with summaries capped at 1200 string
units. On reconnection it republishes unread sessions in these states. Notices
may repeat; consumers should deduplicate by session ID and sequence. Detailed
output remains available through `agent.session`; native IPC is not a second
session database.

## Isolated validation

Swift tests cover relay correlation, reverse-order replies, cancellation,
timeout, pause, standalone unavailability and loopback URL validation.
For packaged end-to-end tests only, `BUDDY_NATIVE_TEST_MODE=1` suppresses native
menu/discovery, real pairing reconnect, pairing UI and permission prompts. Audit
output goes to `/dev/null`. It does not simulate the command executors.

With that mode enabled, `BUDDY_TEST_DEVICE_URL=ws://127.0.0.1:<port>/api/buddy/ws`
connects to a mock server using synthetic buddy ID `test-buddy` and Bearer token
`test-token`, without loading or saving real pairing credentials. The path may be
omitted. Only explicit IPv4 loopback, `ws`, a valid port, no credentials, no query
and no fragment are accepted. The override has no effect outside test mode.
