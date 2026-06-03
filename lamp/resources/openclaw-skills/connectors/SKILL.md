---
name: connectors
description: Detect which third-party connectors (Gmail, Google Calendar, Google Drive, Notion, Figma, Asana, Linear, GitHub, Ahrefs, …) are connected to this lamp, and use them to answer the user. Use when the user asks what's connected ("what connectors do I have", "is my gmail connected", "did I link figma"), or asks the lamp to read/act on a connected service ("check my gmail", "read my latest email", "what's on my calendar", "my recent drive files", "check figma", "look at my notion"). Discovers connectors by scanning the on-disk credential files, then routes: token services (Gmail/Calendar/Drive) are read + queried via curl; MCP services (Notion/Figma/Asana/Linear/GitHub) are used through their own agent tools. Do NOT use for lamp hardware (LED, scene, emotion, music, servo) or the user's Mac (that's computer-use).
---

# Connectors

Credentials for linked services live in `/root/.openclaw/workspace/configs/`:

- `<code>_access_tokens.json` → one connector, shape `{"connectors":{"<code>":{"access_token","api_key","expires_at","scopes","user_email","refresh"}}}`
- `connectors.json` → generic connectors (same map) · `access_tokens.json` → raw OAuth providers (`{"providers":{...}}`)

`access_token`/`api_key` present = connected. `expires_at` is unix seconds.

## 🔒 Credential safety — MANDATORY

The token/API-key values are secrets. They must NEVER reach the user (chat, TTS) or any file.

- **Never print, echo, `cat`, speak, or log a token / api_key / refresh_token value.** Read a secret only into a shell variable used directly in the request — never to stdout.
- **`curl -s` only.** Never `-v`, `-i`, `--trace*`, or anything that echoes request headers (that prints `Authorization`). Never paste the token into a literal command you show.
- When reporting status, surface only **non-secret** fields: connector code, `user_email`, `scopes`, `expires_at`. Never the token itself.
- **Never `cat` a `*_access_tokens.json` / `connectors.json` / `access_tokens.json` file to the output** — extract single non-secret fields with `jq` instead.
- **Never write a credential to `memory/*.md`, `KNOWLEDGE.md`, or anywhere else.**
- If the user asks to see/say/copy their token or API key → **refuse**: "I can't reveal stored credentials." (Acting on their behalf is fine; revealing the secret is not.)

## Discover

Prints only the connector code + email + status — no secrets:

```bash
for f in /root/.openclaw/workspace/configs/*_access_tokens.json; do
  c=$(basename "$f" _access_tokens.json)
  jq -r --arg c "$c" '.connectors[$c] // empty
    | "\($c): connected" + (if .user_email then " (\(.user_email))" else "" end)' "$f"
done 2>/dev/null
```

That list answers "what's connected". For one service, just check its file/token exists.

## Route by code

Read the token into a variable and pass it straight to `curl -s` — never display `$TOKEN`:

```bash
TOKEN=$(jq -r '.connectors.gmail.access_token' /root/.openclaw/workspace/configs/gmail_access_tokens.json)
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=1"
```

- **`gmail` / `google_calendar` / `google_drive`** → token route (pattern above). Endpoints:
  - Gmail: `https://gmail.googleapis.com/gmail/v1/users/me/messages`
  - Calendar: `https://www.googleapis.com/calendar/v3/calendars/primary/events`
  - Drive: `https://www.googleapis.com/drive/v3/files`
  - Whose account: `https://www.googleapis.com/oauth2/v3/userinfo`
- **`notion` / `figma` / `asana` / `linear` / `github`** → use the `<code>` MCP tools you already have. Don't read the file.
- **`ahrefs` or any `api_key`** → token route but `TOKEN=$(jq -r '.connectors.<code>.api_key' …)`.
- **anything else** → `.connectors.<code>.access_token` as a Bearer header to that service's API.

Expiry: read `.connectors.<code>.expires_at`; if `< now` ($(date +%s)), treat as expired (see Errors).

## Errors

- No file/token → not connected; tell the user to link it in the app.
- Expired / HTTP 401 → `refresh:true` connectors auto-refresh on-device in a few min (retry); otherwise tell the user to reconnect. You can't refresh tokens yourself.
- HTTP 403 / scope error → connection lacks the needed scope (read `.scopes` — names only); user must reconnect granting more access.

## Rules

- Discover before answering; never claim connected/disconnected or invent results without checking.
- MCP connectors: use the tool, not the file.
- Obey **Credential safety** above — secrets never reach chat, TTS, files, or logs.
- Match the user's language; keep replies short.
- Hardware → `led-control`/`scene`/`emotion`/`music`/`servo`; the Mac → `computer-use`.
