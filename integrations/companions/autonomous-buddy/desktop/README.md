# Autonomous Buddy desktop

Local agent manager built with Electron, React and TypeScript. The three-column workspace combines projects/worktrees/sessions, agent transcripts or an interactive terminal, and Git changes/files/recent commits. It is the main window of one Autonomous Buddy app. The same application bundle embeds the Swift helper for pairing, the device WebSocket and native computer-use.

From this directory, with Node.js 22.12+ and npm available:

```bash
npm install
npm run lint
npm test
npm run build
npm run test:e2e
npm start
```

`npm install` rebuilds `node-pty` for Electron; native build tools may be required (Xcode Command Line Tools on macOS). `npm start` loads the existing build. `npm run dev` builds and starts the app; it does not run a hot-reload server. The Electron smoke test requires a desktop session and an existing build.

Install and sign in to your agent CLI separately. Buddy discovers `codex` and `claude` on the inherited `PATH`; launch from a terminal with that PATH. CLI availability does not prove authentication. Terminal sessions use `$SHELL`, falling back to `/bin/sh`.

Open a local project, select its worktree, then create a Codex, Claude Code or Terminal session. Send a prompt and subsequent messages in the same agent tab. Separate sessions can run concurrently. Stop terminates the selected process; closing the app stops all managed processes. Projects and bounded transcripts survive restart; shell processes do not.

For a local macOS installation, run `make build` and then `make install` from this directory. Both Electron and Swift are built for the current Mac architecture and installed as one `/Applications/Autonomous Buddy.app`. The same commands work from the parent `autonomous-buddy/` directory. `make install` rebuilds both components before staged installation; it validates the bundle ID, signature and embedded executable, gracefully stops the previous apps, and archives the old Swift app outside `/Applications` under `~/Library/Application Support/AutonomousBuddy/LegacyBackups/` with a `.disabled` suffix. Existing manager state and native pairing data stay in place. Failed replacement restores the previous bundles. `make open` launches it. The bundle is ad-hoc signed by default when built from this directory; `DEV_ID_APP` enables Developer ID signing. The parent Makefile detects an available identity. Build/install never notarizes; distribution uses the explicit `dmg-signed` workflow. Packaged startup restores CLI search paths from the login shell (5-second limit), with inherited and standard paths as fallback.

State lives in Electron's `userData/manager.json` (normally `~/Library/Application Support/Autonomous Buddy/manager.json` on macOS). `BUDDY_DATA_DIR` overrides the data directory for isolated testing. Removing a project from Buddy removes its saved sessions and transcripts, not its files or Git worktrees.

See [architecture and limitations](../docs/agent-manager.md) and [tài liệu tiếng Việt](../docs/vi/agent-manager_vi.md). This is a local development app, not a signed release. The embedded helper uses private child-process IPC; lamp routing into managed agent sessions and remote access are not implemented. Tests use simulated agent output; they do not establish live paid-provider compatibility.

The Swift executable is bundled at `Contents/Resources/native/AutonomousBuddy`; Electron owns its lifecycle and starts it with `--embedded-helper`. Quit stops the helper and managed sessions. Native development and the original Swift release recipes remain available only through explicit `native-*` targets in the parent Makefile; users install the unified app. Native Accessibility and Screen Recording permissions remain macOS-managed and may need granting for the newly packaged app.
