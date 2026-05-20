# CAD

Mechanical source files for the Lumi lamp.

Large CAD binaries (`*.stp`, `*.step`, `*.stl`, `*.f3d`) are **not committed**.
They live in this folder locally (gitignored) and are mirrored to **Mega.nz**.
The table below holds the public share links — that is the source of truth for
anyone outside the hardware team.

## Files

| File | Format | Mega link | Uploaded |
|------|--------|-----------|----------|
| `lamp-v3.stp` | STEP AP214 | [mega.nz/file/g6Jh2LrK](https://mega.nz/file/g6Jh2LrK#iaRlF5b5EPSEbbXJQtpJvBzzr75UyfcDWKKQsQq95yc) | 2026-05-20 |

## Uploading a new revision

1. Drop the file in `hardware/cad/` (it is gitignored).
2. Make sure MEGAcmd is logged in: `mega-whoami` should print your account.
   If not, see [Auth setup](#auth-setup) below.
3. Run the helper:

   ```bash
   scripts/upload-cad.sh hardware/cad/lamp-v3.stp
   ```

   The script uploads to `/lumi-cad/<filename>` on Mega and prints a public
   share link.
4. Paste the link into the table above, update the date column, and commit
   `hardware/cad/README.md`.

## Auth setup

MEGAcmd uses email + password (Mega has no traditional API key). Log in once
per machine — credentials are stored in `~/.megaCmd/` and the session persists.

```bash
mega-login your-email@example.com 'your-password'
```

Prefix the command with `!` in Claude Code so it runs in your shell, not in
the assistant prompt — the password then never appears in the conversation
log.

To sign out: `mega-logout`.

## Changelog

- **v3** (2026-05-20) — initial STEP export, uploaded to Mega.
