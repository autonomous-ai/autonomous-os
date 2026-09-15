# App updates

The packaged macOS app checks for updates 30 seconds after launch and every 6 hours thereafter. It downloads available updates in the background and never forces an immediate restart. Development builds and native test mode do not make network update requests. There is currently no preference toggle for automatic checks or downloads.

## Check and install

**Check for Updates…** is available in the Buddy menu bar and Electron application menu even while Agent Manager and Settings remain hidden. It does not open the workspace.

| Menu label | Behavior |
| --- | --- |
| Check for Updates… | Check now; a manual check reports an up-to-date version or an error in a dialog. |
| Checking for Updates… | Disabled while checking. |
| Downloading Update… | Disabled while downloading. |
| Restart to Update… | The download is ready; choose Restart or Later in the confirmation dialog. |
| Restarting… | Disabled during shutdown and installation. |

An automatic download shows a desktop notification when ready; clicking it offers the restart confirmation. Restart stops active agent sessions and native device control through the same cleanup path as normal Quit, then installs and relaunches the app. Sessions are not automatically resumed. **Later** defers the immediate restart; the downloaded update still installs on the next normal quit. Saved pairing and application data remain in their existing locations.

Buddy **0.0.21 and earlier** have no updater: install a newer DMG manually once to receive this feature. Adding updater source does not itself publish an OTA release or change the version.

## Feed and packaging

The Electron main process owns the built-in `autoUpdater` (Squirrel.Mac), using a static HTTPS JSON feed at:

```text
https://storage.googleapis.com/s3-autonomous-upgrade-3/os/ota/autonomous-buddy/<arch>/latest.json
```

`<arch>` is `arm64` or `x64`, matching the running app architecture. The feed points to a signed, notarized app **ZIP** for automatic installation. **DMG** files and the shared architecture-specific OTA metadata continue to provide manual downloads; a DMG is not an auto-update payload. The packaged app version is read from `VERSION_AUTONOMOUS_BUDDY`, so update comparisons use the release version rather than the desktop package development version. See [release signing](release-signing.md) for publishing and retry rules.

The Swift embedded helper receives private `update_status` requests with `{label, enabled}` and emits the allowlisted `check-updates` menu event to Electron. Update status persists across native menu rebuilds; it does not expose an updater API to the renderer or open Agent Manager.

Local unit, build and smoke checks do not establish a successful production upgrade. Before distributing the first updater-enabled release, verify a signed installed app against a newer signed release on the matching Mac architecture, including restart, helper shutdown and pairing retention.

## Local validation

Run the separate desktop test runners from `desktop/`:

```bash
npx vitest run tests/*.test.ts
node --test tests/signing-identity.test.mjs scripts/test-update-feed.mjs scripts/test-update-permissions.mjs
```

Run publisher regression tests from the repository root:

```bash
python3 scripts/release/tests/test_upload_autonomous_buddy.py
```

The current `npm test` command also discovers Node test files through Vitest and can fail on that inherited runner mismatch; the explicit commands above keep the two runners separate.

For an opt-in real Squirrel installation smoke test, first run `make build` in the Buddy directory, then:

```bash
cd desktop
node tests/update-electron-smoke.mjs
```

This requires macOS and a usable Developer ID signing identity. The test signs disposable app copies with a unique test bundle ID, uses a loopback-only test feed and native test mode, and checks actual replacement/relaunch plus a preserved data sentinel. It does not replace the installed Buddy or use the production feed. This command documents the verification path; it is not a claim that a production upgrade has passed.
