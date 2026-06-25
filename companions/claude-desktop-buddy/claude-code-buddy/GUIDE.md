# Claude Code Buddy — Setup Guide

Let your device react to what Claude Code is doing — Task Done, high usage, and "Claude needs you" — right from across the room.

---

## What you need

- A **device** (e.g. a lamp) already set up through its own setup flow — Wi‑Fi connected and powered on.
- The device and your Mac on the **same LAN** (same Wi‑Fi / subnet), so the plugin can reach the device's `:5002` API.
- The device running its **companion service** (`claude-desktop-buddy`) — it ships with the device software and is what exposes `:5002`.
- **Claude Code** signed in with **OAuth** (a Pro/Max login, not an `ANTHROPIC_API_KEY`) — the usage push reads your OAuth usage.

---

## Install

Open your terminal and run:

```bash
claude plugins marketplace add https://raw.githubusercontent.com/autonomous-ai/autonomous-os/main/companions/claude-desktop-buddy/claude-code-buddy/.claude-plugin/marketplace.json
```

```bash
claude plugins install claude-code-buddy
```

Then **restart Claude Code** (exit and reopen).

---

## Connect your device

1. Make sure the device is powered on and on the same WiFi as your computer.
2. Open Claude Code and type:

```
connect my device
```

3. Claude runs discovery — it looks up the device on your LAN (mDNS, then a quick subnet scan) and saves it to your config. There are no codes to read off a screen.
4. That's it — the device is connected.

Behind the scenes this writes `~/.config/claude-code-buddy.json` with the device's hostname and last-known IP, and sets it as your default. You don't need to touch that file.

---

## macOS: allow Local Network access

Recent macOS (Sonoma / Sequoia) blocks apps from reaching devices on your local
network until you grant **Local Network** permission. The plugin's scripts run
under `python3`, so if that permission is missing, Python can't reach the device
— even though the device is online and reachable from `curl`.

**Symptoms:**

- `connect my device` says it can't find the device, even though it's powered on
  and on the same WiFi.
- After connecting, **nothing arrives on the device** — no Task Done, no usage,
  no ping — because the hooks' network calls are silently blocked.

**Fix:** open **System Settings → Privacy & Security → Local Network**, and turn
it **on** for the app that runs Claude Code (Terminal, iTerm, or the Claude app).
Then restart that app and try `connect my device` again.

To confirm Python can reach the device, run (replace the IP with yours):

```bash
python3 -c "import urllib.request as u; print(u.urlopen('http://192.168.1.50:5002/health', timeout=2).read())"
```

A `{"status":"ok",...}` response means Local Network access is working. An error
like `No route to host` while `curl` to the same address succeeds is the
tell-tale sign the permission is still off.

---

## What happens next

Once connected:

- After each response, Claude sends a **Task Done** push so the device signals you're free to look back at the result.
- When your usage (5-hour or 7-day) reaches your threshold — **80%** by default — Claude also pushes your current usage so you can pace your work.

You can change the threshold just by asking Claude in plain language:

- "warn me earlier" / "show my usage sooner"
- "set my warning threshold to 60"
- "only warn me near the limit"

Claude updates the config for you. Or edit `~/.config/claude-code-buddy.json` by hand:

```json
{
  "usage_threshold": 80
}
```

Set it lower (e.g. `60`) to get pushed more often, or higher (e.g. `90`) to only hear about it when you're near the limit.

You can also:

- Say `notify my device` to send a custom message.
- Type `/claude-code-buddy:usage` to push your usage immediately.

---

## "Claude needs you" ping

When Claude **needs your approval** to run something (a yes/no prompt) or your input has been **left idle**, the device pings you with a distinct cue — separate from the Task Done signal — so you'll notice even if you've stepped away.

---

## Turning notifications on/off

You don't always want a sound or every ping. Just ask Claude in plain language and it updates the config for you (takes effect immediately, no restart):

- "mute my device" — keep the events, silence the sound
- "stop the task done notification" — no more Task Done push
- "stop pinging me when Claude needs me" — no more "Claude needs you" ping
- "turn everything back on" — re-enable all

Or edit `~/.config/claude-code-buddy.json` by hand (all default `true`):

```json
{
  "sounds_enabled": true,
  "task_done_enabled": true,
  "notify_enabled": true
}
```

- `sounds_enabled: false` → events still fire, but without a sound cue
- `task_done_enabled: false` → no Task Done push (usage pushes still fire)
- `notify_enabled: false` → no "Claude needs you" ping

---

## Status

The plugin sends events now. The device-side receivers (`/claude-code/notify` and `/claude-code/usage`) exist and log each push, but the device-side rendering (the HAL bridge to LED / display / voice) is still being rolled out, so depending on your device firmware those events may not produce feedback yet. Connecting and config changes work either way.

---

## Update the plugin

```bash
claude plugins update claude-code-buddy@claude-code-buddy
```

Restart Claude Code after updating.

---

## Uninstall

```bash
claude plugins uninstall claude-code-buddy
```
