---
name: connectors
description: Detect which third-party connectors (Gmail, Google Calendar, Google Drive, Notion, Figma, Asana, Linear, GitHub, Ahrefs, …) are connected to this lamp, and use them to answer the user. Use when the user asks what's connected ("what connectors do I have", "is my gmail connected", "did I link figma"), or asks the lamp to read/act on a connected service ("check my gmail", "read my latest email", "what's on my calendar", "my recent drive files", "check figma", "look at my notion"). Discovers connectors by scanning the on-disk credential files, then routes: token services (Gmail/Calendar/Drive) are read + queried via curl; MCP services (Notion/Figma/Asana/Linear/GitHub) are used through their own agent tools. Do NOT use for lamp hardware (LED, scene, emotion, music, servo) or the user's Mac (that's computer-use).
---

# Connectors

Credentials for linked services live in `/root/.openclaw/workspace/configs/`:

- `<code>_access_tokens.json` → one connector, shape `{"connectors":{"<code>":{"access_token","api_key","expires_at","scopes","user_email","refresh"}}}`
- `connectors.json` → generic connectors (same map) · `access_tokens.json` → raw OAuth providers (`{"providers":{...}}`)

`access_token`/`api_key` present = connected. `expires_at` is unix seconds.

## Discover

```bash
for f in /root/.openclaw/workspace/configs/*_access_tokens.json; do
  jq -r 'to_entries[]?|.value|keys[]? // empty' "$f"; basename "$f" _access_tokens.json
done 2>/dev/null
```

That list answers "what's connected". For one service, just check its file/token exists.

## Route by code

- **`gmail` / `google_calendar` / `google_drive`** → read the token, call the API with `Authorization: Bearer <token>`:
  - Gmail: `https://gmail.googleapis.com/gmail/v1/users/me/messages`
  - Calendar: `https://www.googleapis.com/calendar/v3/calendars/primary/events`
  - Drive: `https://www.googleapis.com/drive/v3/files`
  - Whose account: `https://www.googleapis.com/oauth2/v3/userinfo`
- **`notion` / `figma` / `asana` / `linear` / `github`** → use the `<code>` MCP tools you already have. Don't read the file.
- **`ahrefs` or any `api_key`** → `Authorization: Bearer <api_key>`.
- **anything else** → token is `.connectors.<code>.access_token`; send it as a Bearer header to that service's API.

Token + expiry: `jq -r '.connectors.gmail.access_token' …`; if `.expires_at < now`, treat as expired.

## Errors

- No file/token → not connected; tell the user to link it in the app.
- Expired / HTTP 401 → `refresh:true` connectors auto-refresh on-device in a few min (retry); otherwise tell the user to reconnect. You can't refresh tokens yourself.
- HTTP 403 / scope error → connection lacks the needed scope (check `.scopes`); user must reconnect granting more access.

## Rules

- Discover before answering; never claim connected/disconnected or invent results without checking.
- MCP connectors: use the tool, not the file.
- Never print raw tokens/keys. Match the user's language; keep replies short.
- Hardware → `led-control`/`scene`/`emotion`/`music`/`servo`; the Mac → `computer-use`.
