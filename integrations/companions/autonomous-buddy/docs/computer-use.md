# Computer use on the paired Mac

Computer use lets the agent running on an Autonomous device complete tasks in the user's Mac applications through Buddy. It covers native apps, browsers, custom interfaces, and work spanning apps. Agent management, which manages local projects and CLI sessions, is a separate feature.

## Ownership and execution loop

```text
User objective → device agent + computer-use skill
              → device-local OS HTTP API → paired WebSocket → Buddy Swift on Mac
              ← command result / Accessibility observation / screenshot
              → observe, act, verify, continue until the requested outcome
```

The device runtime owns reasoning, context, clarification, and completion. Buddy supplies native execution and observations; it does not independently plan a workflow. `skills/computer-use/SKILL.md` selects synchronous commands for every task needing returned information, dependent actions, or verification. Opening a URL completes only a request to open that URL; a room-search request must continue through parameters, search, and reading actual results.

The installed skill's `scripts/buddy.py` calls `http://127.0.0.1:5000/api/buddy/command` on the **device**, checks both the OS response envelope and Buddy result, and returns matched command JSON. It creates unique command IDs and never retries automatically. `--params-file` accepts a JSON file to avoid interpolating user or screen text into shell commands. Helper timeouts default to 15000 ms, accept 500–60000 ms, and allow another 10 seconds for the HTTP response. Native commands without a supplied timeout default to 5000 ms. These are command deadlines, not whole-task limits.

Inline `/buddy/exec/<action>` HW markers remain available for simple self-contained actions. Their results do not return to the model's reasoning loop. They are unsuitable for observations or dependent action sequences.

## Native observation and reference actions

`get_ui_tree` accepts optional `app` (running app name or bundle ID), `max_nodes` (1–500, default 150), and `max_depth` (1–30, default 12). Without `app`, it observes the foreground application. It returns `snapshot_id`, `app`, `bundle_id`, `pid`, `frontmost`, `truncated`, `expires_in_ms`, and flat `nodes`.

Nodes include `ref`, optional `parent_ref`, `role`, supported `actions`, `secure`, and available `title`, `description`, `value`, `help`, `enabled`, `focused`, and `bounds_global_points`. Bounds are `{x,y,width,height}` in global desktop points. Text values are capped at 500 characters per attribute. Secure controls and their descendants do not expose their text/value. Traversal uses bounded child reads, a 3-second traversal budget, and a 0.2-second AX messaging timeout per element. A truncated or incomplete tree is not proof that a control is absent; use another observation or screenshot.

`perform_ui_action` requires `snapshot_id`, `ref`, and `ui_action`:

| Action | Behavior |
|---|---|
| `press` | Invoke the observed element's supported AXPress action. |
| `focus` | Set AXFocused when the element supports it. |
| `set_value` | Set AXValue to string `value`, at most 20000 characters, when supported; secure fields are excluded. |

References remain within Buddy's process. They expire after 30 seconds, a new observation, another mutating command, or an attempted reference mutation after validation. The observed process must still be foreground. Buddy rechecks PID, role, title, enabled state, and action support. Obtain a fresh observation after every action or error. References reduce ambiguity but cannot make a changing UI transactional; the agent must still verify the outcome. Setting a value does not necessarily submit a form or trigger every app-specific event.

## Screenshots and input

`list_displays` returns display IDs, global point origins/dimensions, backing pixel dimensions, and scale. `screenshot` accepts an active `display_id`, `scale` from 0.01 through 1 (default 1), and `return_format` of `path`, `base64`, or `both` (default `path`). Output dimensions must be 1–16384 pixels per axis and at most 40 million pixels. JPEG captures have unique names under `~/Library/Application Support/AutonomousBuddy/screenshots/`; Buddy retains the newest 20 managed captures.

The device helper requests base64, validates and decodes the response, and saves a unique JPEG plus geometry metadata on the device. The first capture creates a private task directory and returns `capture_dir`; reuse it with `--output-dir` for subsequent captures. Each task directory retains at most 50 helper-owned image/metadata pairs; abandoned task directories require explicit cleanup. It returns `local_image_path` and `metadata_path`; the Mac's path is only `mac_image_path`. The runtime must load `local_image_path` with a tool that returns actual image content to the model. Printed JSON or base64 alone is not vision. For a text-only runtime, use the auxiliary vision fallback below; if neither path is available, use adequate Accessibility observations or explicitly report the missing image capability. Device captures have separate task-managed cleanup; Mac's 20-file retention does not clean device files.

Mouse input uses global CGEvent **points**, with a top-left origin and possibly negative secondary-display coordinates. Use each screenshot's actual encoded dimensions and `image_to_global_points`:

```text
global_x = origin_x + image_x * scale_x
global_y = origin_y + image_y * scale_y
```

Account for any resizing in the image viewer before using image coordinates. Refresh screenshots and geometry after display-layout changes. Do not mix a capture with another capture's transform.

| Input | Validated bounds |
|---|---|
| Mouse coordinates | Finite numeric values from -1000000 to 1000000; booleans are rejected. |
| `click_at` | `button`: left/right/middle; `clicks`: integer 1–3, default 1. |
| `scroll` | Integer `delta_x`/`delta_y`: -100000 to 100000; optional cursor placement requires both `x` and `y`. |
| `drag` | `from`/`to` point objects; `duration_ms`: integer 50–10000, default 300. |
| `type_text` | At most 100000 UTF-16 units; `delay_ms`: integer 0–1000, default 15. |
| `key_combo` | At most six keys, exactly one supported non-modifier key. |

Typing, smooth movement, repeated clicks, and dragging check cancellation between events. Interrupted dragging releases the mouse at the last delivered position. Input already delivered may still have changed the app.

## Auxiliary vision for text-only runtimes

`POST /api/buddy/observe` is device-local only and accepts `question` (required, 1–2000 Unicode characters), optional positive uint32 `display_id`, and optional `scale` (0.01–1, default 0.5). The helper exposes this as `buddy.py observe --question 'What is visible and where is the search field?'`. It captures the paired Mac once with native screenshot timeout 15000 ms, then calls the existing configured auxiliary image model with a desktop-specific prompt. Capture and description share the caller's cancellation and an 80-second total budget; the helper waits up to 90 seconds. No automatic retry occurs.

The response uses the standard OS envelope with `data: {description, screenshot}`. Screenshot metadata retains image dimensions and coordinate transform; base64 is omitted. The server checks the matched response ID, native success, JPEG MIME/header, dimensions against metadata, maximum 12 MiB image payload, and existing dimension limits before sending the image to the configured model. Request JSON is limited to 16 KiB. Invalid inputs return HTTP 400; capture/vision failures return 502, and deadline failures return 504, with the standard error envelope.

This is model-derived visual evidence, not direct perception by the text-only agent and not an action executor. Ask focused questions about visible controls/text and request image-pixel centers when needed; convert coordinates with the returned screenshot transform. Treat uncertainty explicitly and re-observe after an action. The screenshot is sent to the device's configured vision provider, using the same auxiliary model catalog/configuration as existing image descriptions. It is never described as a device-camera photo.

## Permissions, concurrency, and recovery

`desktop_info` is a read-only preflight returning `protocol_version: 2`, `paused`, `accessibility`, `screen_recording`, `frontmost_app`, capabilities, and up to 100 running apps with `pid`, `name`, `bundle_id`, and `active`; `apps_truncated` indicates omitted entries. It does not prompt for permissions. It works while paused, but still rejects overlapping work as busy.

Grant the running Buddy app Accessibility permission for AX and keyboard/mouse control, and Screen Recording permission for screenshots in macOS System Settings → Privacy & Security. Pairing and an active WebSocket are also required. Permission failures, disconnected Buddy, paused Buddy, and unavailable AX content are distinct outcomes; repeated input cannot repair missing access.

The native dispatcher allows one active ordinary command and rejects overlapping work as busy. `cancel_command` with `{"id":"<active-command-id>"}` is handled while that command runs. Its `cancel_requested` result means a matching active command was asked to stop, not that prior effects were undone. Local Pause cancels active work and rejects new ordinary commands. Resume does not resume the old user task automatically.

OS WebSocket writes are serialized. Responses are matched to their originating connection and request; a reconnect fails pending work rather than allowing a replacement connection to satisfy old requests. HTTP cancellation/timeout causes best-effort cancellation of the corresponding native command. Cancellation is cooperative, and synchronous OS calls may take time to return.

A timeout, disconnect, or cancellation leaves partial effects possible. Never automatically retry a consequential mutation. Re-observe the application and determine what happened before choosing another action. A successful native response means the command executed; it does not establish semantic success such as correct search results, saved content, or a completed user workflow.

## Manual acceptance checklist

These are acceptance scenarios, **not a record of passed live tests**. Use disposable files and authorized actions, record the runtime/model, Buddy build, permission state, display geometry, commands, observations, and final evidence.

- **Native app:** create and edit a disposable note/document; inspect the UI with AX, identify controls by context, and verify final content and save location.
- **Browser:** open a requested search site, obtain missing destination/date or equivalent parameters, submit a real search, and report results actually read. Opening the site alone fails the scenario.
- **Cross-app:** read known sample data in one app, transfer a summary into another, switch back when clarification changes the request, and verify the destination contents.
- **Custom UI:** interact with a canvas or control lacking useful AX content through actual screenshots and pointer input; verify the visible result.
- **Multiple displays/Retina:** repeat targeting at full and reduced screenshot scale on primary and secondary displays, including negative origins; verify intended controls receive input.
- **Stale references:** change focus, wait past 30 seconds, or reuse a consumed snapshot; check rejection and recovery through fresh observation.
- **Permissions/disconnect:** revoke a permission or disconnect Buddy; verify a specific failure and no invented success. Reconnect and observe before resuming.
- **Cancellation:** stop long typing/dragging through cancel and local Pause; confirm no stuck mouse button, inspect partial effects, and ensure no blind retry.
- **Concurrency:** issue overlapping commands; verify busy rejection and no interleaved input. Reconnect while a command is pending and check that an old response cannot satisfy a new request.
- **Whole-task completion:** introduce a clarification, slow loading, and an unexpected dialog. Verify the agent preserves the objective, changes approach after repeated failure, and reports either evidenced completion or the exact remaining blocker.

## Application launch and focus

`open_app` requests activation once and returns `pid`, `bundle_id`, `app`, `activation_requested`, `frontmost`, and `requires_observation`. `open_url` returns `opened` and `browser`, activation/observation flags, and app/focus metadata when the running handler is available. `frontmost` describes the response-time foreground process, not a guarantee that the target window is visible on the screenshot's display. The app may still be loading, a dialog may be present, or the user may switch apps. Observe the intended window/display before typing.

Explicit browser selection supports Chrome, Safari, Firefox, Arc, Edge, and Brave aliases. An unsupported or uninstalled requested browser fails; Buddy does not silently substitute the default. With no `browser` or `browser: "default"`, Buddy uses the registered URL handler and checks whether macOS accepted the open request. Rejection is an error. `opened: true` confirms an accepted request, not page readiness or a completed workflow. App launch does not repeatedly reclaim focus from the user.

### Natural speech, follow-ups, and multiple displays

A user can say “Open Airbnb and find a stay in Da Nang” without naming Buddy or its tools. The computer-use skill retains the whole search objective and missing details across short replies such as “this weekend, two people.” It preserves the destination and guest count, resolves relative dates from trusted current date/timezone information, and asks for exact stay nights when “weekend” is ambiguous. Corrections update the pending task; stop requests cancel it. The same behavior applies to native apps: “write this in Notes” creates and verifies the note, while “rename it” refers to the previously established item only when that reference is clear. Finder actions operate on the Mac, not the device filesystem.

Before reading a target app visually, the agent enumerates displays. `is_main` marks the primary display and does not establish where the active app's window is visible. When available, Accessibility window `bounds_global_points` are matched to display rectangles. Otherwise the agent inspects plausible displays once each using explicit `display_id` values until it locates the target. It retains that display ID and the latest screenshot transform, and refreshes discovery if the target disappears or the arrangement changes. An unrelated app on the primary display is not evidence that the requested app failed to open. The agent does not move windows merely to simplify observation.

The natural-voice evaluation cases in `skills/computer-use/evals/natural-voice.json` cover lodging follow-ups, corrections, Notes, Finder, cross-app summaries, cancellation, and a three-display case where Buddy is on primary display 1, Chrome results on display 4, and another app on display 5. Those IDs belong to the test scenario, not a fixed layout. Scenario definitions and syntax checks do not establish a live device pass.

A short request to open or interact with a website/app targets the paired Mac even when the user omits “Mac” or “computer”. Skill discovery includes these spoken requests. A browser installed on the headless device is not a substitute; pure information research remains separate.

The skill instructs the agent to compare search and text-entry parameters against retained user wording before dispatch, including place names and dictated content; a successful command for a substituted destination is a failed task.

## Opening Mac files and folders

`open_path` resolves a filesystem `path` on the Mac. Use `{"path":"~/Downloads"}` to open Downloads without knowing the Mac username. Accepted paths are absolute, `~`, or prefixed with `~/`; relative paths, `~otheruser`, URL strings, NUL characters, nonexistent targets, and paths over 16384 UTF-8 bytes are rejected. Home expansion uses the Mac process's home directory, never the device's home. Existing files and folders are supported; the executor does not create or modify filesystem contents.

Optional `mode` is `open` (default) or `reveal`. Opening uses the registered app; optional `app` selects an installed app by name or bundle ID, for example `{"path":"~/Documents/draft.txt","app":"TextEdit"}`. An explicit missing app fails instead of falling back. `{"path":"~/Downloads/report.pdf","mode":"reveal"}` asks Finder to select the item; `app` cannot be combined with `reveal`. These operations use NSWorkspace and do not require Accessibility or AppleScript.

Results contain resolved `path`, `is_directory`, `mode`, `opened` or `reveal_requested`, and activation/observation metadata. Reveal is a request because the Finder API does not return semantic confirmation. Opening a file may launch its associated application; verify the intended window and contents before reporting completion or typing. `desktop_info.capabilities` advertises `open_path` on supporting Buddy builds.
