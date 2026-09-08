# Desktop settings

Open **Autonomous Buddy → Settings…** in the macOS app menu, press **⌘,**, or use the sidebar's Workspace settings button. Settings opens a full page over the current workspace. Agent processes, session views, terminals, and unsent drafts remain mounted underneath. **Back to app** or **Escape** returns to the workspace. Keyboard focus stays inside Settings, and the workspace underneath is inert while the page is open.

The left navigation contains only working sections: Appearance, Agents, and Computer & device. Search filters actual setting labels and descriptions across those sections. Matching advanced terminal controls automatically become visible during search; an empty result shows a clear message.

## Appearance

| Setting | Choices / limits | Default |
| --- | --- | --- |
| Theme | System, Dark, Light | System |
| UI zoom | 75%–150%, steps of 5% | 100% |
| IDE font | System default, Sans serif, Monospace | System default |
| Terminal font size | 10–24 px, steps of 1 | 13 px |
| Terminal font family | SF Mono, Menlo, Monaco, monospace | SF Mono |
| Terminal line height | 1.0–2.0, steps of 0.1 | 1.4 |
| Cursor blink | On / off | On |
| Cursor style | Block, Bar, Underline | Block |
| Agent usage footer | Show / hide | Show |
| Workspace status bar | Show / hide | Show |

Theme and fonts apply to the workspace as well as Settings. System theme follows macOS appearance changes. Zoom scales the Electron interface. The View menu provides Zoom In (⌘=), Zoom Out (⌘−), and Reset Zoom (⌘0), using the same persisted value. Terminal appearance updates existing xterm instances and refits their dimensions without restarting the shell or clearing scrollback. Missing local fonts fall back to monospace; no font assets are downloaded or copied from Orca.

Changes save automatically through the main-process `settings`, `updateAppearance`, and `onSettings` APIs. Number fields accept a draft while typing and save on blur or Enter, normalized to the displayed bounds and step. Plus/minus controls save immediately. Controls remain disabled during initial load or a read error, preventing a failed settings fetch from silently replacing saved preferences with defaults. A visible error reports failed updates. Invalid saved settings are preserved; startup shows an error with the settings location and recovery instructions rather than silently replacing the file.

## Agents and computer controls

Agents shows actual CLI availability for Codex, Claude Code, and Terminal. Coding sessions currently use full local access without per-command approval prompts; this panel explains that behavior rather than offering an unimplemented approval toggle. The selected project's registration can be removed after confirmation. Running sessions prevent removal; the registration, sessions, and stored history are removed while project files stay on disk.

Computer & device opens the existing native-control panel in the same app for pairing, Accessibility/Screen Recording permissions, pause/resume, and activity. These permissions remain independent of agent CLI accounts.

## Source audit and scope

The layout and workflows were reviewed against Orca's `AppearancePane.tsx`, `AppearanceInterfaceSection.tsx`, and `TerminalAppearanceSection.tsx` in the local upstream checkout. Buddy uses original UI code. This page does not claim Orca's entire settings catalog: custom theme imports, language packs, arbitrary font discovery, remote/mobile settings, and extra sidebar sections are not exposed here.

Validation uses desktop `npm run lint`, `npm run build`, settings-store tests, and the packaged Electron smoke workflow. See [workspace-ui.md](workspace-ui.md) for workspace and tab behavior.
