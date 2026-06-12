# Vision loop — see-think-act on the user's Mac

This reference unlocks the **synchronous vision loop**: take a screenshot of the user's Mac, look at it, decide where to click, fire the click, screenshot again. Use this **only** when the marker-based actions in the parent `SKILL.md` cannot do the job — for example, the user asks you to click a UI element that has no stable label, drag a slider, locate a window by appearance, or read text off the screen.

The marker pattern (`[HW:/buddy/exec/<action>:{...}]`) is fire-and-forget and cannot return data. Vision needs return values, so it uses a different transport: **bash + curl against the device's local API** with the full Command schema.

## When to load this reference

Load this file (i.e. follow these instructions instead of the parent SKILL.md's "fire-and-forget" pattern) when the task requires:

- **Visual reasoning** — "find the blue button in the toolbar", "click the X on that dialog", "what's on my screen right now?".
- **No stable accessibility label** — `click_button` only works when macOS Accessibility exposes the label. Web content in Chrome/Safari often does not.
- **Multi-step UI flows that depend on prior state** — fill a form whose fields change order between visits, navigate a settings panel.
- **Reading text off the screen** — capture and OCR / describe an error dialog, summarise a webpage section.

Do NOT load this for tasks the parent skill already handles (open app, open URL, type into focused field, keyboard shortcuts, named-button clicks). Those are faster and far more reliable than vision.

## Transport

Call the device's local HTTP endpoint via bash:

```bash
curl -s -X POST http://127.0.0.1:5000/api/buddy/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"<action>","params":{...},"timeout_ms":15000}'
```

The endpoint is localhost-only (device-internal). The device dispatches over the buddy's persistent WebSocket and returns the buddy's response **synchronously** wrapped in the standard envelope:

```json
{"status": 1, "data": {"id": "...", "ok": true, "result": {...}, "error": null, "duration_ms": 213}, "message": null}
```

- `status: 1` + `data.ok: true` → use `data.result`.
- `data.ok: false` → read `data.error`. Common errors: `"no buddy connected"`, `"buddy paused by user"`, `"unknown action"`, `"timeout after Nms"`, `"Screen Recording access required"`, `"Accessibility access required"`.
- `status: 0` → device-level failure (buddy not paired, bad params).

## Coordinate system — read this once before clicking

**Rule: always screenshot at ~1280px wide.** This is the resolution Claude was trained against for computer use, and clicks land more accurately when the image fed to your reasoning matches that scale. Larger screenshots cause undershoot; much smaller ones lose small UI like close buttons. Compute the `scale` param from the native pixel width (one-time `list_displays` call at session start):

```
scale = 1280 / displays[0].pixel_width        # use is_main:true display
```

Examples:
- 13" MacBook Retina (2560×1600 native pixels) → `scale ≈ 0.50`
- 16" MacBook Retina (3456×2234 native pixels) → `scale ≈ 0.37`
- External 4K (3840×2160 native pixels) → `scale ≈ 0.33`
- Non-Retina 1080p (1920×1080 native pixels) → `scale ≈ 0.67`

Click targets are in **CGEvent points** (top-left origin), not pixels. Once the screenshot is exactly 1280px wide, the conversion from a pixel coord you read in that image to a click point is:

```
click_x_points = pixel_in_screenshot_x * point_width / 1280
click_y_points = pixel_in_screenshot_y * point_height / 1280   # use point_width ratio, height follows
```

`point_width` and `point_height` come from `list_displays` (it returns `width`/`height` in points and `pixel_width`/`pixel_height` in pixels). For the single-display common case the math collapses to:

```
ratio = point_width / 1280        # cache this for the whole session
click_x_points = pixel_x * ratio
click_y_points = pixel_y * ratio
```

If you forget the ratio (or skip the scale-to-1280 step), the click lands at the wrong spot — usually outside the target on Retina.

For multi-display setups, call `list_displays` first and pick the display the user is asking about (typically `is_main: true`). Pass that display's `id` to `screenshot` and use the same display's `x`/`y` origin offset when computing click coords.

## Available vision actions

| Action | Returns / Effect | Key params |
|---|---|---|
| `screenshot` | `{path, width, height, display_id, display_scale, bytes, mime, image_b64?}` — JPEG q=0.8 to keep LLM vision token cost low | `display_id` (default main), `scale` (default 1.0 — shrink for token budget), `return_format` `"path"` / `"base64"` / `"both"` |
| `list_displays` | `{displays:[{id,is_main,x,y,width,height,pixel_width,pixel_height,scale}], count}` | — |
| `cursor_pos` | `{x, y, screen_height, backing_scale}` (CGEvent space) | — |
| `click_at` | `{clicked, x, y, button, clicks}` | `x`, `y` (points), `button` `"left"`/`"right"`/`"middle"`, `clicks` |
| `mouse_move` | `{moved, x, y, smooth}` | `x`, `y`, `smooth` |
| `drag` | `{dragged, from, to}` | `from:{x,y}`, `to:{x,y}`, `duration_ms` |
| `scroll` | `{scrolled, delta_y, delta_x}` | `delta_y`, `delta_x`, optional `x`/`y` to position cursor first |
| `read_clipboard` | `{text}` | — |
| `write_clipboard` | `{wrote}` | `text` |

The non-vision actions from the parent SKILL.md (`open_app`, `open_url`, `type_text`, `key_combo`, `notification`, `click_button`) also work over `/api/buddy/command` if you want a synchronous confirmation. Prefer them when the action does not require seeing the screen.

## Loop template

```
1. screenshot                          → know what's on screen
2. (look at the image / reason)        → decide next single action
3. fire that action (click_at / scroll / type_text / key_combo / drag)
4. screenshot again                    → verify the action landed
5. repeat from step 2, or stop when done
```

Some practical rules for the loop:

- **One action per iteration.** Don't batch multiple clicks before re-screenshotting — the screen state may have shifted (modal appeared, focus changed, animation in progress).
- **Evaluate after every step.** After each action, take a screenshot and carefully evaluate if you achieved the right outcome. Show your thinking explicitly: *"I have evaluated step X — the modal opened as expected, ready for step Y"* or *"I clicked but nothing changed; the button label was misread, retrying with neighbouring coord"*. Don't assume an action worked just because the click endpoint returned `ok:true` — only the next screenshot proves it.
- **Prefer keyboard shortcuts when the target is risky.** Dropdowns, scrollbars, tiny icons, and chrome (close/minimise dots) are notoriously hard to click accurately at 1280×800. If a keyboard shortcut exists (`cmd+w` to close tab, `cmd+,` to open preferences, `cmd+f` to focus search, `tab` + `enter` to navigate forms), use `key_combo` instead of `click_at`. More reliable, no coord math.
- **Wait briefly after navigations.** After `open_url` or a click that opens a new view, sleep ~500-1000 ms before the next screenshot so the page can render. In bash: `sleep 1`.
- **Stop the loop on success or after ~6-8 iterations.** Vision is unreliable; if you can't make progress in that many steps, tell the user what you saw and ask for a more specific instruction. Looping past 8 attempts almost never succeeds — accept the limitation honestly rather than spending more tokens on guesswork.
- **Stop on error.** If `data.ok: false`, surface the error to the user — do not retry blindly. `"Screen Recording access required"` and `"Accessibility access required"` need the user to grant permission in System Settings; no amount of retrying will fix that.
- **Don't narrate every step.** Speak one short caring sentence at the start ("Let me have a look") and one at the end ("Done — clicked the Submit button."). All the screenshot/click reasoning stays in `thinking`.

## Reading the screenshot

The screenshot result includes `path` (filesystem path on the user's Mac, visible only to the buddy app) and optionally `image_b64`. The device-side LLM cannot read the user's filesystem, so to actually **see** the image you must request base64.

**Recipe — call once at session start, then reuse:**

```bash
# 1. Discover the display so you know what scale to ask for.
curl -s -X POST http://127.0.0.1:5000/api/buddy/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"list_displays","params":{},"timeout_ms":3000}'
# Returns: {displays:[{id, is_main, width, height, pixel_width, pixel_height, scale,…}],…}
# Pick the is_main display. Compute scale = 1280 / pixel_width. Cache it.

# 2. Every screenshot uses that scale.
curl -s -X POST http://127.0.0.1:5000/api/buddy/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"screenshot","params":{"scale":<cached_scale>,"return_format":"base64"},"timeout_ms":15000}'
```

Why not `scale: 1.0`? On a 4K Retina display a full-res JPEG is still ~500KB-1MB and ~5-8 million pixels — Claude undershoots clicks because the trained dim is much smaller, plus you burn tokens. `scale: 0.5` (the old default) was an approximation that works for 13" Retina but is too small on a 16" and too large on a non-Retina display. Anchor on the 1280px target instead.

The `image_b64` field is a base64-encoded JPEG (quality 0.8). Treat it as image input in your next reasoning step.

## Example flows

### A — Click a button with no accessibility label (e.g. a custom web button)

Session state (computed once at start): display is 3840×2160 native pixels, 1920×1080 points (display_scale=2.0). So `scale = 1280/3840 ≈ 0.33`, and the click-conversion ratio is `point_width / 1280 = 1920/1280 = 1.5`.

```bash
# 1. Screenshot at the cached scale.
curl -s -X POST http://127.0.0.1:5000/api/buddy/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"screenshot","params":{"scale":0.33,"return_format":"base64"},"timeout_ms":15000}'
# → image is 1280×720 (close to target). I see "Subscribe" at pixel (800, 400) in the image.
# → click target in points = (800*1.5, 400*1.5) = (1200, 600).

# 2. Click.
curl -s -X POST http://127.0.0.1:5000/api/buddy/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"click_at","params":{"x":1200,"y":600,"button":"left"},"timeout_ms":3000}'

# 3. Verify (and start the next step's reasoning from this image, not the previous one).
sleep 1
curl -s -X POST http://127.0.0.1:5000/api/buddy/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"screenshot","params":{"scale":0.33,"return_format":"base64"},"timeout_ms":15000}'
```

### B — Read text off a dialog

```bash
# 1. screenshot
# 2. look at image → transcribe the dialog text
# 3. reply to user: "Your Mac is showing a dialog that says: '...'."
# No further actions — pure read flow.
```

### C — Multi-step: open Settings, find Bluetooth, toggle off

Prefer **deep links over visual navigation** whenever possible:
```
open_url x-apple.systempreferences:com.apple.Bluetooth-Settings.extension
```
If no deep link exists, fall back to the vision loop:
```
1. open_app "System Settings"
2. (wait 1s, screenshot)
3. type_text "Bluetooth" + key_combo ["return"]   ← search bar is focused on launch
4. (wait 1s, screenshot)
5. click_at on the toggle (read coords from screenshot)
6. screenshot to verify toggle changed state
```

### D — Locate a window by appearance, then drag it

```
1. screenshot
2. identify the window's title bar coords
3. drag from (title_x, title_y) to (target_x, target_y) with duration_ms 400
4. screenshot to verify
```

## Reliability — set expectations

Vision-driven computer use is **inherently unreliable**: Anthropic's Computer Use benchmark and OSWorld both report ~22-40% success per multi-step task. Errors compound: a 4-step flow at 80% per step is 41% end-to-end.

To stay useful:

- **Prefer Tier 1 (API integrations) over vision** when the user's goal has an API. Joining a Google Meet via `https://meet.google.com/<id>` is 100% reliable; navigating Google Calendar visually to find and click "Join" is maybe 30%.
- **Prefer Tier 2 (markers / deep links / shortcuts) over vision** when the task fits. `key_combo ["cmd","space"]` to open Spotlight always works; visually finding the Spotlight icon and clicking it is fragile.
- **Tier 3 (vision) is the last resort.** It's powerful but brittle. If you find yourself looping more than ~6 times, stop and tell the user honestly what you tried and what you saw.
- **Tell the user when the task is on the edge of feasibility.** "I can try clicking through Calendar, but it's not very reliable — want me to just open the meeting URL directly if you have it?"

## Error responses to handle

| `data.error` | Meaning | What to do |
|---|---|---|
| `no buddy connected` | The buddy app is offline | Tell the user the Mac isn't reachable; ask them to wake their Mac or check the Buddy menu bar icon |
| `buddy paused by user` | User paused the buddy from the menu bar | Tell the user the buddy is paused; ask them to resume from the menu bar |
| `Screen Recording access required …` | macOS TCC not granted | Tell the user to grant Screen Recording in System Settings → Privacy & Security, then ask again |
| `Accessibility access required …` | macOS TCC not granted for clicks/typing | Same — grant Accessibility in System Settings → Privacy & Security |
| `timeout after Nms` | Action exceeded its own timeout | Surface to the user; do not retry the same action automatically |
| `unknown action: X` | Typo or unsupported action | Pick a supported action from the table above |

## Notes

- **One short spoken sentence per turn.** Keep the user in the loop ("Let me take a look", "Found it — clicking now", "Done"). All the screenshot/coordinate math stays in `thinking`.
- **Don't expose paths or coordinates to the user.** They want to know whether the task succeeded, not pixel (1234, 567).
- **Match the user's language** in the spoken confirmation (English in, English out; Vietnamese in, Vietnamese out).
- **Stop and ask** if the screen is locked, the user is mid-typing in a sensitive app (password manager, banking), or the task drifts beyond what they originally asked. Don't keep clicking through unfamiliar UI.
