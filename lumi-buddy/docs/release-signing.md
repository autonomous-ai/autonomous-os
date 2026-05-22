# Lumi Buddy — release signing & notarization

This is the handover doc for whoever owns the Apple Developer enrolment. Once the one-time setup is done, every release boils down to:

```bash
cd lumi-buddy
export DEV_ID_APP="Developer ID Application: <Your Org> (<TEAMID>)"
export NOTARY_PROFILE=lumi-notary
make dmg-signed
```

The output `dist/LumiBuddy-<version>.dmg` is signed, notarized, and stapled — users mount it, drag the app to Applications, double-click, and macOS opens it without any Gatekeeper warning or right-click dance.

The ad-hoc `make dmg` path still works for local builds and informal sharing; this doc only covers the production path.

## What changes vs the ad-hoc build

| | Ad-hoc (`make dmg`) | Production (`make dmg-signed`) |
|---|---|---|
| Signing identity | None (`-` placeholder) | Developer ID Application cert from Apple |
| Hardened runtime | Off | **On** (`--options runtime`, required by Apple) |
| Secure timestamp | No | **Yes** (`--timestamp`) |
| Notarized by Apple | No | **Yes** (notarytool submit + wait) |
| Stapler ticket | No | **Yes** (`stapler staple`) — works offline too |
| First-launch UX | Right-click → Open Anyway | Just double-click |
| TCC reset on rebuild | Every build (cdhash changes) | Stable across rebuilds (identifier-based cert) |
| User permission grants | Re-grant Accessibility + Screen Recording each release | Granted once, persists across releases |

Stable TCC is the single biggest user-facing win — without it you re-burn through ~3 permission dialogs every time a tester gets a new build.

## One-time setup (the dev who owns the cert does this)

### 1. Enrol in the Apple Developer Program

`https://developer.apple.com/programs/enroll/` — $99/year. Individual or organisation account both work; organisation is preferable so the cert isn't tied to a single Apple ID.

After enrolment, note the **Team ID** (10-char alphanumeric, e.g. `ABCDE12345`) — it appears in your Apple Developer account header and is needed below.

### 2. Create a Developer ID Application certificate

The straight path is from inside Xcode (download Xcode if you don't have it):

1. Xcode → **Settings → Accounts** → "+" → sign in with the enrolment Apple ID.
2. Pick the team → **Manage Certificates…**.
3. "+" → **Developer ID Application**. Xcode generates the CSR + downloads the `.cer` + installs the private key into your login Keychain in one step.

Manual path (no Xcode) — only if Xcode is not available:

1. Keychain Access → **Certificate Assistant → Request a Certificate From a Certificate Authority…** → save CSR to disk.
2. `https://developer.apple.com/account/resources/certificates` → "+" → **Developer ID Application** → upload CSR → download `.cer`.
3. Double-click the `.cer` → installs into login Keychain; private key was generated in step 1 alongside the CSR.

Verify the cert is usable:

```bash
security find-identity -v -p codesigning
```

Look for `Developer ID Application: <Your Org> (<TEAMID>)` in the output. The full quoted string is what you pass as `DEV_ID_APP` to `make`.

### 3. Set up notarytool credentials

Notarization runs against an Apple-issued **app-specific password**, not your iCloud password. Create one once:

1. `https://account.apple.com/account/manage` → Sign-In and Security → **App-Specific Passwords** → Generate (label it `lumi-buddy-notary` or similar).
2. Copy the password (format `xxxx-xxxx-xxxx-xxxx`).
3. Store the credential trio (Apple ID, Team ID, app-specific password) in the macOS Keychain so `notarytool` can pull it without prompts:

```bash
xcrun notarytool store-credentials lumi-notary \
  --apple-id "your-apple-id@example.com" \
  --team-id "ABCDE12345" \
  --password "xxxx-xxxx-xxxx-xxxx"
```

`lumi-notary` is the profile alias — pass it as `NOTARY_PROFILE` to `make`. You can pick any name; just stay consistent.

Smoke test:

```bash
xcrun notarytool history --keychain-profile lumi-notary
```

Empty history is fine — it means auth works.

## Per-release flow

```bash
cd lumi-buddy

# Persist these in your shell rc once, or export per session.
export DEV_ID_APP="Developer ID Application: Autonomous Inc (ABCDE12345)"
export NOTARY_PROFILE=lumi-notary

# Optional: bump VERSION in the Makefile if this is a new release.

make dmg-signed
```

The make target does, in order:

1. `swift build -c release` — production binary.
2. Generate the app icon if missing (`make icon` chain — SF Symbol placeholder unless you replace it with a designed PNG).
3. Bundle `dist/LumiBuddy.app` with the icon + Info.plist.
4. `codesign` the app with Developer ID, hardened runtime, secure timestamp.
5. `hdiutil create` the DMG (drag-to-Applications layout).
6. `codesign` the DMG with Developer ID.
7. `xcrun notarytool submit … --wait` — uploads to Apple, blocks 1-5 minutes until verdict.
8. `xcrun stapler staple` — embeds the notarization ticket so Gatekeeper can verify offline.
9. `spctl --assess` — local Gatekeeper dry-run, prints `accepted` on success.

End result is `dist/LumiBuddy-<version>.dmg`. Ship that file.

## Verifying a build before shipping

```bash
# 1. App signature is well-formed.
codesign --verify --deep --strict --verbose=2 dist/LumiBuddy.app

# 2. Gatekeeper accepts the app.
spctl --assess --type execute --verbose=4 dist/LumiBuddy.app
#   expected: "accepted source=Developer ID notarized"

# 3. DMG itself has a stapled ticket.
xcrun stapler validate dist/LumiBuddy-<version>.dmg
#   expected: "The validate action worked!"

# 4. Real Gatekeeper dry-run on the DMG.
spctl --assess --type open --context context:primary-signature --verbose=4 dist/LumiBuddy-<version>.dmg
#   expected: "accepted source=Notarized Developer ID"
```

All four should pass before you upload anywhere.

## Common failure modes

**`errSecInternalComponent` during codesign.** Your private key is missing or locked. Open Keychain Access → login → search for "Developer ID Application" → confirm the private key sibling exists. If only the cert is there, you imported the `.cer` on a different machine than the one that generated the CSR — re-do step 2 on this machine.

**`Hardened Runtime is not enabled` from notarytool log.** The `--options runtime` flag is missing. The Makefile target sets this; if you ran `codesign` by hand, re-sign with `--options runtime`.

**`The signature does not include a secure timestamp`.** Missing `--timestamp` flag. Same fix — the Makefile sets it; manual codesign needs it explicitly.

**Notarization status `Invalid` with log mentioning `disallowed-entitlement`.** You're using an entitlement Apple doesn't allow for Developer ID distribution. Buddy currently doesn't set entitlements, so this should not happen. If it does, fetch the full log:

```bash
xcrun notarytool log <submission-id> --keychain-profile lumi-notary
```

…and check which entitlement was rejected.

**Notarization status `Accepted` but Gatekeeper still warns on the user's Mac.** The DMG wasn't stapled. Either re-run `make notarize` against the existing DMG (re-staples) or rebuild with `make dmg-signed`.

**User reports "app is damaged".** Usually means the quarantine xattr is set and the staple ticket is missing or invalid. Have the user run `xattr -d com.apple.quarantine /Applications/LumiBuddy.app` as a one-off; permanent fix is to ship a stapled DMG.

## When to re-notarize

You need a fresh notarization any time the .app binary or the DMG changes — i.e. every release. Notarization is bound to the exact bits; you can't "transfer" a ticket from one build to another. The Makefile handles this automatically by running the full chain.

Stapling is offline-capable, so users who first install when offline still get the no-warning experience as long as the DMG itself has the ticket embedded.

## Things this doc deliberately does NOT cover

- **Mac App Store distribution.** Different cert (`Apple Distribution`), App Sandbox required, separate submission flow via App Store Connect. Out of scope for now.
- **Sparkle / auto-update.** Buddy currently has no built-in update channel; releases are manual DMG drops to users. Add Sparkle later if release cadence picks up.
- **CI signing.** Doable (GitHub Actions with cert + notarytool keychain profile encrypted as secrets), but the current handoff assumes one dev signs locally. Set up CI when build cadence justifies it.
