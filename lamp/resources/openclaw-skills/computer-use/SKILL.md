---
name: computer-use
description: Control the user's Mac via the paired Lamp Buddy companion app — open/close apps, navigate URLs in Chrome, type text into focused fields, fire keyboard shortcuts, show desktop notifications, write to clipboard, click named UI buttons via macOS Accessibility. Also covers vision-driven tasks (screenshot, find/click unlabelled UI, read text off the screen, drag) — those load `reference/vision.md` for the synchronous see-think-act loop. Use when the user explicitly asks Lamp to do something on their COMPUTER (e.g. "open Chrome", "go to Gmail", "join Meet", "close Slack", "type … into my Mac", "copy … to clipboard", "click the blue button on my screen", "what's on my Mac right now?"). Do NOT use for hardware control of the lamp itself (LED, scene, emotion, music, servo) — those are separate skills. Do NOT use if no Mac is paired (the lamp web UI shows pairing status under the Buddy card).
---

# Computer Use (Mac via Lamp Buddy)

## Quick Start

Lamp Buddy is a small macOS app the user installs on their Mac. Once paired with the lamp via the Buddy card on the Monitor web UI, it lets Lamp launch apps, open URLs, type text, fire keyboard shortcuts, and click UI elements **on the user's actual Mac**.

This skill emits inline markers that Lamp fires asynchronously while TTS speaks the confirmation:

```
[HW:/buddy/exec/<action>:<params-json>]
```

The marker hits `/api/buddy/exec/<action>` on the lamp, which dispatches over the buddy's persistent WebSocket. Fire-and-forget — no response is awaited (TTS continues immediately).

## Workflow

1. Determine the user's intent and pick one or more actions from the table below.
2. Build the marker(s) — flat params JSON only (no nested objects).
3. Place markers at the **start of the reply**, then add a short confirmation that TTS will speak.
4. If no Mac is paired, say so and tell the user to set it up via the Lamp web UI Buddy card.

### When to load `reference/vision.md` instead

The marker pattern below covers ~90% of computer-use requests: launching apps, opening URLs, typing into the focused field, keyboard shortcuts, named-button clicks. It is fire-and-forget — fast, but cannot return data.

Load `reference/vision.md` and follow its synchronous bash/curl loop **only** when the task requires actually seeing the screen:

- "Click the blue button in the toolbar" / "click the X on that dialog" (no stable accessibility label)
- "What's on my screen right now?" / "Read me the error dialog"
- "Drag the slider to the middle" / "move that window over here"
- Multi-step UI navigation where each step depends on what appears next

Do NOT load vision for tasks the marker actions already handle — vision is slower and far less reliable (~22-40% per multi-step task).

## Examples

Input: "Open Chrome on my computer"
Output: `[HW:/buddy/exec/open_app:{"app":"Google Chrome"}]` Opening Chrome on your Mac.

Input: "Open Gmail"
Output: `[HW:/buddy/exec/open_url:{"url":"https://gmail.com"}]` Opening Gmail.

Input: "Join the Meet at abc-defg-hij"
Output: `[HW:/buddy/exec/open_url:{"url":"https://meet.google.com/abc-defg-hij"}]` Joining the meeting.

Input: "Search Google for 'best pho Saigon'"
Output: `[HW:/buddy/exec/open_url:{"url":"https://www.google.com/search?q=best+pho+Saigon"}]` Searching Google.

Input: "Close Slack"
Output: `[HW:/buddy/exec/close_app:{"app":"Slack"}]` Closed Slack.

Input: "Open Spotify"
Output: `[HW:/buddy/exec/open_app:{"app":"Spotify"}]` Opening Spotify.

Input: "Type 'hello world' into the active field"
Output: `[HW:/buddy/exec/type_text:{"text":"hello world"}]` Typed.

Input: "Open Spotlight"
Output: `[HW:/buddy/exec/key_combo:{"keys":["cmd","space"]}]` Spotlight open.

Input: "Close the current Chrome tab"
Output: `[HW:/buddy/exec/key_combo:{"keys":["cmd","w"]}]` Tab closed.

Input: "Remind me in 5 minutes about the meeting"
Output: `[HW:/buddy/exec/notification:{"title":"Meeting in 5 min","body":"Get ready"}]` Reminder set.

Input: "Copy leo@example.com to my clipboard"
Output: `[HW:/buddy/exec/write_clipboard:{"text":"leo@example.com"}]` Copied to clipboard.

Input: "Click the Submit button"
Output: `[HW:/buddy/exec/click_button:{"label":"Submit"}]` Clicked.

Input: "Hello" / "What time is it?"
Output: Do NOT use this skill. Reply normally.

Input: "Turn the lamp yellow"
Output: Do NOT use this skill — use **led-control** skill instead.

Input: "Reading mode" / "Make it cozy"
Output: Do NOT use this skill — use **scene** skill instead.

## Available actions

### `open_app` — launch a macOS app

```
[HW:/buddy/exec/open_app:{"app":"Google Chrome"}]
```
- `app` (required): app display name (e.g. "Google Chrome", "Spotify", "Calculator") OR bundle id (e.g. "com.google.Chrome").

### `close_app` — quit a macOS app

```
[HW:/buddy/exec/close_app:{"app":"Slack"}]
```
- `app` (required): app display name. Runs AppleScript `tell app to quit` under the hood; macOS may show a per-app Automation prompt on first close.

### `open_url` — open a URL in the default browser

```
[HW:/buddy/exec/open_url:{"url":"https://gmail.com"}]
```
- `url` (required): full URL with `https://`.
- `browser` (optional): `"chrome"`, `"safari"`, `"firefox"`, `"arc"`, `"edge"`, `"brave"`. Omit → default browser.

Tip: many apps expose deep links — use them instead of clicking through UI:
- Gmail compose: `https://mail.google.com/mail/?view=cm&to=X&subject=...&body=...`
- Google search: `https://www.google.com/search?q=...`
- Meet room: `https://meet.google.com/<id>`
- YouTube search: `https://www.youtube.com/results?search_query=...`
- GitHub repo: `https://github.com/<owner>/<repo>`

### `type_text` — type text into the focused field

```
[HW:/buddy/exec/type_text:{"text":"hello world"}]
```
- `text` (required): text to type.
- `delay_ms` (optional): per-character delay, default 15.
- Needs macOS Accessibility permission (one-time grant in System Settings).

### `key_combo` — fire a keyboard shortcut

```
[HW:/buddy/exec/key_combo:{"keys":["cmd","space"]}]
```
- `keys` (required): array of strings. Modifiers: `"cmd"`/`"command"`, `"shift"`, `"opt"`/`"option"`/`"alt"`, `"ctrl"`/`"control"`, `"fn"`. Plus exactly one key: `"a"`–`"z"`, `"0"`–`"9"`, `"return"`, `"escape"`, `"tab"`, `"space"`, `"delete"`, `"left"`/`"right"`/`"up"`/`"down"`, `"f1"`–`"f12"`, etc.
- Needs Accessibility permission.

### `notification` — show a macOS desktop notification

```
[HW:/buddy/exec/notification:{"title":"Meeting in 5","body":"Get ready"}]
```
- `title` (required), `body` (optional).
- First call may prompt for notification permission.

### `write_clipboard` — set clipboard text

```
[HW:/buddy/exec/write_clipboard:{"text":"some text"}]
```
- `text` (required). User can paste with `Cmd+V` afterwards.

### `click_button` — Accessibility-based click by label

```
[HW:/buddy/exec/click_button:{"label":"Submit"}]
```
- `label` (required): visible button text. Matches via macOS Accessibility API.
- `app` (optional): restrict search to a specific app (display name or bundle id).
- Works well for **native apps** (Settings, Finder, Notes, Calculator). For web content (Chrome / Safari), coverage is inconsistent — Chrome may not expose all button labels in its accessibility tree.

## Combining actions

Markers fire in order. Useful patterns:

**Send a tweet** — open compose URL, type text, submit:
```
[HW:/buddy/exec/open_url:{"url":"https://twitter.com/compose/tweet"}][HW:/buddy/exec/type_text:{"text":"hello"}][HW:/buddy/exec/key_combo:{"keys":["cmd","return"]}]
```
*Caveat:* markers fire sequentially but do NOT wait for page load between steps. If the URL takes time to load, the `type_text` may land in the wrong field. Prefer a single-marker action when timing is uncertain.

**Spotlight → open Notes**:
```
[HW:/buddy/exec/key_combo:{"keys":["cmd","space"]}][HW:/buddy/exec/type_text:{"text":"Notes"}][HW:/buddy/exec/key_combo:{"keys":["return"]}]
```

## Error handling

- **No buddy paired** (`no buddy connected`): respond "No Mac is paired with the lamp yet. Open the Monitor page → Buddy card to pair one." Do NOT fire any markers.
- **Timeout / connection error**: respond "I couldn't reach your Mac — the Buddy app may be offline."
- **Unknown action**: not possible if you only use the actions listed above. Stick to the table.

## Rules

- **Markers must appear at the START of the reply**, before the TTS sentence. Lamp parses and strips them before reading.
- **No nested JSON** in marker params (the marker regex doesn't support nested `{}`). All actions above take flat params.
- **One action per marker.** Don't try to batch multiple ops into a single marker body.
- **Don't use this skill for lamp hardware** (LED, scene, emotion, audio playback on the lamp speaker, servo, display) — those are separate skills.
- **Don't fire `screenshot`, `click_at`, `scroll`, `mouse_move`, `drag`, `read_clipboard`, `cursor_pos`, `list_displays`** through inline markers. Those need return values (vision loop) and use a different transport. If the task needs visual reasoning (find an unlabelled button, drag a slider, read text off the screen), load `reference/vision.md` and follow its synchronous bash/curl pattern instead.
- **Match the user's input language** in the TTS confirmation (English in, English out; Vietnamese in, Vietnamese out). Keep the TTS reply to one short sentence.
- **If the user asks for lamp-side actions** ("turn yellow", "play music", "show emotion"), redirect to the appropriate skill (`led-control`, `music`, `emotion`, `scene`).

## Output template

```
[HW:/buddy/exec/<action>:<flat-params-json>] <short confirmation sentence>
```

Examples:
- `[HW:/buddy/exec/open_url:{"url":"https://gmail.com"}] Opening Gmail.`
- `[HW:/buddy/exec/close_app:{"app":"Slack"}] Closed Slack.`
- `[HW:/buddy/exec/key_combo:{"keys":["cmd","tab"]}] Switched app.`
