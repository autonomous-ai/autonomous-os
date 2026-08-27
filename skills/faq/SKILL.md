---
name: faq
description: Explain how to run and use this repository's local Web UI, with detailed guidance for every Settings section and its visible controls. Use for questions about web development, Monitor, Setup, or Settings.
---

# Local Web UI FAQ

Answer questions about the UI from the controls that actually exist. Reply in
the user's language. Start with the page, control, and outcome; then mention
the relevant side effect. Do not invent a button, field, provider, or feature
that is not described here.

## First identify the context

`localhost` means the computer running the browser. It is not the robot.

### Developing the frontend locally

The local frontend is Vite. From the repository root, run:

```bash
make web-install
LAMP_PROXY=http://<device-lan-ip> make web-dev
```

Open `http://localhost:5173`. `LAMP_PROXY` forwards the local UI's `/api` and
`/hw` requests, including WebSockets, to the device. Without it, Vite can show
the page shell, but Monitor and Settings cannot load or save live device data.

### Using the UI already installed on a device

Open `http://<device-lan-ip>/` from the same network. Use the raw LAN IP shown
by the device or router; do not guess the IP and do not present `.local` as the
primary address. `/monitor` opens the dashboard; `/setting` opens Settings.
The old `/edit` route redirects to `/setting`.

If the UI asks for the admin password, the user must enter it in the local UI.
Never request a password, API key, bot token, or header value in chat.

## Navigation and saving rules

The left sidebar has Monitor and Settings. The search field filters items by
name or group; `Enter` opens the first result and `Esc` clears it. Direct links
are relative to the current UI address, for example `/setting#wifi`.

Some advanced sections require the **Debug** toggle in the top bar. Explain
what a control does before suggesting Debug; do not turn it on merely to make a
user experiment with an unfamiliar setting.

Most Settings sections use the **Save Changes** button at the upper right. It
is enabled only after an edit. A saved secret is shown as configured rather
than revealed; use its edit control and enter a replacement only when changing
it. Saving settings that affect voice processing can briefly restart HAL, so
wait for the UI to reconnect before clicking another control.

These areas do not use **Save Changes**:

- **My Voice** and **Face** carry out enrollment/removal with their own buttons.
- **Runtime** uses its own **Switch to …** button.
- **MCP Tools** and **Plugins** apply each Add/Remove/Install action directly.
- **Timezone** applies immediately when a zone is chosen from its picker.

## Detailed Settings guide

### General — `/setting#general`

Use this page for basic device information, admin access, and how the device
listens.

- **Device ID** identifies the device and has a copy button. Treat it as a
  reference value; it is not a setting to change from this page.
- **MAC** is masked until the eye button is pressed. Use it only when a router
  or network administrator needs the hardware address.
- **Admin Password** lets an authenticated operator replace the current login
  password. Enter the new value and confirmation, meet the minimum length shown
  in the form, then use **Save Changes**. Explain that the password protects a
  device with hardware controls; recommend a unique, strong value.
- **Require a wake phrase / speech attention** controls the speech gate. When
  enabled, the device needs an attention trigger before handling speech: an
  accepted wake phrase, a single click, turning toward the lamp while speaking,
  or an enrolled person entering view. The card shows the phrases currently
  accepted, including the current agent name and built-in aliases. With the
  gate off, normal speech is processed directly. Save and allow the voice
  service to restart.

### Wi-Fi — `/setting#wifi`

The current SSID is read-only by default. Press the pencil icon to edit it;
the UI scans and shows a network picker. Use the refresh icon to scan again.
If no result is available, type the SSID manually. Enter or replace the Wi-Fi
password, then press **Save Changes**.

Warn before saving: changing the network can disconnect the current browser.
After the device joins the new network, reconnect using its new LAN IP. Use the
cancel icon to abandon an SSID edit before saving.

### AI Brain — `/setting#llm`

This configures the LLM used by the device.

- **API Key** is the provider credential. Enter it only in the local form.
- **Base URL** is the provider's compatible API endpoint, such as an OpenAI
  compatible endpoint or a self-hosted proxy.
- **Model** is the exact model ID accepted by that endpoint.

Saved values are locked until the user explicitly chooses to edit them. Before
changing any of these, state that the device's responses may stop working if
the endpoint, credential, and model do not match. Save the three values
together, then test with a short chat in Monitor.

### Runtime — `/setting#runtime`

The **Backend** picker selects the agent runtime (currently, as supported by
the device: OpenClaw, Hermes, PicoClaw, Codex, Claude Code, or OpenCode). The
card states which backend is active and gives a short description of the choice.

Choose a runtime and press **Switch to …**. The UI asks for confirmation, stops
the other runtime, restarts os-server, and polls until the device confirms the
new backend. It can take minutes, especially on a first install. Do not press
the button again while it says **Switching…**. This is a separate operation;
**Save Changes** does not switch runtimes.

### My Voice — `/setting#voice`

This optional page enrolls voice samples so the device can recognize an owner.

1. Enter a person **Name**.
2. Record a sample with the microphone control.
3. Review the sample(s), then use the page's enrollment action.

Existing owners show their voice samples. An operator can remove one sample or
remove all voice samples for an owner; removing voice samples preserves that
person's face data. These actions are immediate and do not use **Save Changes**.
Explain that voice enrollment should be done in a quiet place and with the
actual owner speaking naturally.

### Face — `/setting#face`

This optional page enrolls face photos for recognition.

1. Enter the person's **Name**.
2. Use **Take photo** when a camera is available, or choose/upload photos.
3. Use the enrollment action once the selected photos look correct.

The enrolled-owner list lets the operator expand a person to preview photos,
delete one photo, or remove all photos for that person. Confirm destructive
actions explicitly. Never remove the special `unknown` record: it is not a
normal enrolled owner. Face actions apply through their own controls, not
**Save Changes**.

### Voice (text-to-speech) — `/setting#tts`

Use this page to choose how the device speaks.

- **Provider** offers Autonomous proxy, direct OpenAI, direct ElevenLabs,
  local Piper, or **Custom (BYO URL)**. Preset providers set their own Base URL;
  only Custom exposes an editable URL and a vendor-protocol choice.
- The Autonomous proxy also has a **Vendor** picker for OpenAI or ElevenLabs.
- **API Key** is optional for hosted providers: leave it empty to reuse the AI
  Brain key when that setup supports it. Piper does not use a URL or API key.
- Pick a voice from the available list. The language filter narrows compatible
  hosted voices. **Test Voice** plays a sample on the device; it verifies audio
  output but does not save the configuration.

For **Piper (Local — free)**: press **Install engine** first, then download a
voice model. The progress row shows the active device-side download. It keeps
running if the page is reloaded. Press **Use** to select an installed voice,
then **Save Changes** to make it the device's configured voice. **Remove** asks
for a second confirmation and is unavailable for the voice currently in use or
the last remaining installed voice. Do not test a voice until it has finished
downloading.

### Language (speech-to-text) — `/setting#stt`

Choose the language spoken to the device: Auto, English, Vietnamese, Simplified
Chinese, or Traditional Chinese. Then select the speech-recognition provider:

- **Autonomous (reuse AI brain)** uses the AI Brain-compatible endpoint. Its
  key and Base URL are optional because blank values inherit the AI Brain setup.
- **Deepgram** uses a separate Deepgram API key.

Changing providers changes which saved credential is used. Enter the required
key locally and save before testing speech. Use the language that users will
actually speak; it also influences the default voice choices shown in TTS.

### Realtime — `/setting#realtime`

This is the audio-native realtime agent path. Use the **Enabled** checkbox to
turn it on or off. The **Provider** list comes from the device and may include
Gemini Live, OpenAI Realtime, Qwen Omni Realtime, or None. Pick a **Reasoning**
level when the selected provider offers one; lower-cost options appear first.

The API Key and Base URL are optional overrides. Blank fields reuse/derive from
AI Brain configuration where supported. Changing the provider resets invalid
provider-specific options automatically. Save changes, then wait for the voice
service to restart before testing. Do not describe a hidden realtime voice
selector as a usable UI feature.

### Channels — `/setting#channel`

Select one messaging platform in the dropdown and fill its matching fields:

- **Telegram:** Bot Token and User ID.
- **Slack:** Bot Token, App Token, and User ID.
- **Discord:** Bot Token, Guild ID, and User ID.

Existing tokens are never displayed. Use the edit affordance only to replace a
credential, then press **Save Changes**. Explain which platform will receive
messages before a user changes this page; do not paste a token into chat.

### MQTT — `/setting#mqtt`

This optional page configures an MQTT broker: **Endpoint**, **Port**, optional
**Username** and **Password**, plus the **FA Channel** and **FD Channel** values
used by this deployment. Preserve existing channel values unless the user knows
the broker contract. Press **Save Changes** after editing. Explain that an
incorrect broker address or credential disconnects MQTT publishing; this page
does not diagnose a broker by itself.

### MCP Tools — `/setting#mcp`

This page lists remote MCP endpoints that the active agent can call. To add one:

1. Enter a descriptive **Name** and an HTTPS **URL**.
2. Add header rows only when the server requires them, such as an
   `Authorization` header.
3. Press **Add Tool**.

The tool synchronizes to the active runtime and takes effect immediately. The
list shows each endpoint and the number of configured headers; **Remove**
removes it immediately. Treat an authenticated MCP endpoint like a privileged
integration: describe its scope and obtain the user's confirmation before
adding, replacing, or removing one.

### Plugins — `/setting#plugins`

Plugins are standalone Python applications that extend device capabilities via
HAL. The page lists installed plugins, version, description, and status.

- Use **Start** or **Stop** to control an installed plugin.
- **Uninstall** removes it.
- To add one, enter a trusted plugin repository URL under **Install from URL**,
  then press **Install**. Use **Refresh** to reload the status list.

Installing third-party code and uninstalling a plugin are material changes.
Confirm the exact plugin URL or installed plugin with the user first, and do not
claim that an install is complete merely because the UI says it has started.

### Timezone — `/setting#timezone`

Timezone controls local device time, quiet hours, daily history grouping, and
the assistant's interpretation of time. Press the zone field to open its
searchable picker. Search by city, region, or offset (for example `Ho Chi`,
`Asia`, or `+7`), then click a zone or choose it with arrow keys and Enter.

Choosing a zone applies it immediately; there is no separate **Save Changes**
or **Apply** button. The page shows a live local-time preview. No device restart
is required.

## Monitor and Setup FAQs

- `/setup` is the provisioning flow for Wi-Fi, AI Brain, optional messaging,
  and initial access. Channels may be skipped and configured later.
- `/monitor#overview` summarizes system and component status.
- `/monitor#flow` shows the live agent-event flow.
- `/monitor#camera` shows camera features when the device declares camera
  capability.
- Use the section title and visible labels when explaining any other Monitor
  card. If an item is absent, first check capability support and Debug mode.

## Troubleshooting guidance

- A page stuck after Save usually means a device service is restarting. Wait for
  it to reconnect; do not advise repeated Save, Switch, Download, or Update
  clicks.
- If local development UI loads but data fails, check that `LAMP_PROXY` points
  to the device's reachable raw LAN IP and that the browser opened Vite on
  `localhost:5173`.
- If a device UI becomes unreachable after a Wi-Fi change, reconnect to the
  new network and find the device's current LAN IP instead of retrying the old
  address.
- For a missing Settings item, check Debug mode and hardware capability before
  assuming an error.
