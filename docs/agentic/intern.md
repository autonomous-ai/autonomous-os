# Intern Welcome Desk runtime

`intern` is an explicitly selected, externally owned, text-only runtime. It is
not a device brain and does not install, start, stop, or fall back to OpenClaw,
Hermes, or another runtime. Selecting or leaving it updates only
`agent_runtime`; an operator-managed `os-server` restart activates the saved
selection.

Before that restart, the instantiated gateway continues handling channels,
sensing, configuration updates, and startup reconciliation. These effects and
their runtime markers use the active gateway, not the saved selection. Runtime
switch requests still check the saved selection separately to enforce the
operator-managed restart boundary in both directions.

When active, `os-server` binds its restricted HTTP surface to `127.0.0.1` and
constructs no HAL, sensing, MQTT, channel, skill, firmware, or device handlers.
The runtime sends admitted requests only to the fixed bridge at
`http://127.0.0.1:8765/v1/intern`. It does not load or forward credentials,
follow redirects, use proxy settings, attach images or history, or execute the
returned reception route.

## Admission and results

An authenticated administrator can submit an exact text body to
`POST /api/agent/intern/chat` with:

- `operation`: `route`, `reception`, `classify`, or `generate`
- `data_class`: `public` or `business`
- `admission`: the literal `administrator_classified_exact_text`

Unknown, restricted, and secret data are rejected before a bridge request.
Duplicate, mixed-case, unknown, oversized, or invalid JSON fields are rejected.
The accepted response contains a local `run_id`; poll
`GET /api/agent/intern/result/:run_id` for the bounded result. Completion means
only that the bridge request completed. Results are drafts or reception
metadata with `executes_actions: false`; no handoff or action is performed.

Readiness is not fabricated. It becomes true only after a recent, validated
draft response and expires after 30 seconds. Bridge health alone does not claim
model readiness.

All other paths return “not supported by runtime.” Runtime selection remains
available at `GET/POST /api/device/agent-runtime`, requires administrator
authentication, performs no service changes, and reports that restart is
required.
