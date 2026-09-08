# Autonomous Buddy desktop

Local agent manager built with Electron, React and TypeScript. The three-column workspace combines projects/worktrees/sessions, agent transcripts or an interactive terminal, and Git changes/files/recent commits. It is an optional app alongside the existing Swift computer-use companion.

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

State lives in Electron's `userData/manager.json` (normally `~/Library/Application Support/Autonomous Buddy/manager.json` on macOS). `BUDDY_DATA_DIR` overrides the data directory for isolated testing. Removing a project from Buddy removes its saved sessions and transcripts, not its files or Git worktrees.

See [architecture and limitations](../docs/agent-manager.md) and [tài liệu tiếng Việt](../docs/vi/agent-manager_vi.md). This is a local development app, not a signed release. Swift IPC, lamp voice routing and remote access are not implemented. Tests use simulated agent output; they do not establish live paid-provider compatibility.
