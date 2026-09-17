---
name: music
description: Play and stop music through the device's speaker on user request. Routes to Spotify when the Spotify connector is set up (Premium account, streaming scope) and falls back to YouTube otherwise. Triggers on any music request in any language — "play jazz", "sing me something", "mở nhạc", "phát một bài", "mở album yêu thích", "chơi playlist" — the user never has to say the word "spotify" or "youtube".
---

# Music

Play music through the device's speaker. Use this when the user asks to play, sing, or listen to music — no matter which backend the request lands on.

## ⛔ Step 0: pick the backend BEFORE anything else

Every music request routes through one of two backends. Decide up front, before any HW markers or Spotify API calls, based on what the user has connected. Do NOT ask the user which backend to use — pick silently.

1. **Check Spotify.** Is `~/.openclaw/workspace/configs/spotify_access_tokens.json` present AND the `credentials.scope` field contains the substring `streaming`?
   ```bash
   jq -r '.connectors.spotify.credentials.scope // ""' \
     ~/.openclaw/workspace/configs/spotify_access_tokens.json 2>/dev/null \
     | grep -q streaming && echo SPOTIFY || echo YOUTUBE
   ```
2. **If SPOTIFY** → follow the **Spotify path** below. Higher-quality catalog, real album/playlist targeting, works out of the box with the user's own Premium library. Requires the mint-then-play pattern from the `connectors` skill (SKILL.md `Spotify (special case)` section) — read that for the two-step access-token flow. This path does NOT use `[HW:/audio/play:...]` markers — Spotify plays on a Spotify Connect device, not the local `/audio/play` route.
3. **If YOUTUBE** (Spotify not connected, or missing `streaming` scope) → follow the **YouTube path** below. Free, no auth. Uses `[HW:/audio/play:{"query":...}]` markers as documented in the sections after "Workflow — YouTube path".

Which backend the agent chose is invisible to the user: TTS just says "Playing X" or a short confirmation, no mention of Spotify vs YouTube.

## Workflow — Spotify path

### ⛔ DO NOT touch the Spotify Web API by hand. Use `spotify-play` — always.

`/usr/local/bin/spotify-play` is a self-contained CLI installed on every intern-v2 device. It reads the connectors file, mints a fresh access_token from the stored refresh_token (rotating when Spotify returns a new one), picks a Spotify Connect device, searches, and starts playback in ONE call. Hand-rolling curl to `api.spotify.com` from this skill fails every time because the field on disk that looks like a Bearer token is actually a refresh_token — that mistake is what this wrapper exists to prevent.

**Use these one-liners as `exec` commands. Nothing else.**

```bash
spotify-play "Bohemian Rhapsody Queen"           # search + play a track
spotify-play --album "Rumours Fleetwood Mac"     # album as context
spotify-play --playlist "chill mix"              # playlist
spotify-play --device "intern"                   # target Connect device by name match
spotify-play --pause                             # pause
spotify-play --resume                            # resume
spotify-play --next                              # skip
spotify-play --status                            # print currently playing
spotify-play --devices                           # list Connect devices
```

Success line: `playing: <name> — <artist>  (<type>, on <device>)`. Read it out to the user through the Reply Format section below (still use the emotion marker; no `[HW:/audio/play:...]` on this path because Spotify Connect is already playing).

If `spotify-play` prints one of these, **stop** and speak the user-facing message on the right — do not retry, do not curl by hand:

| Wrapper stdout starts with | Say to user |
|---|---|
| `spotify not connected` / `spotify credentials incomplete` | *"Bạn kết nối lại Spotify trong Settings giúp tao nha."* |
| `spotify mint failed: invalid_grant` / `invalid_client` | *"Spotify của mày cần kết nối lại — vào Settings → Spotify."* |
| `no Spotify Connect device available` | *"Bạn mở Spotify trên điện thoại hoặc máy tính trước rồi mở lại nha."* |
| `spotify play failed: account needs Premium` | *"Spotify của mày không có Premium, tao không phát được — YouTube thay hen?"* — then retry via the YouTube path. |
| `no <type> matched query` | Report to the user + suggest they name a specific song/album/artist. |

### The historical Web API details (reference only — DO NOT copy into `exec`)

**The value in `.api_key` (and mirror `.access_token`) on disk is a REFRESH TOKEN, not an access token.** Spotify's Web API (`api.spotify.com`) rejects it with 401. You MUST first trade it at `accounts.spotify.com/api/token` for a short-lived access_token, then Bearer that on `api.spotify.com`. Skipping this is the #1 way this skill fails — the symptom is always "401 across the board even though the connector shows connected".

Run this entire block as ONE `exec` — do NOT split into "read token" then "use it" (the token expires after ~1h, so caching between turns is unsafe). The block also rotates the refresh_token when Spotify hands back a new one, so 180-day expiry never fires:

```bash
CFG=/root/.openclaw/workspace/configs/spotify_access_tokens.json

# The writer may put the refresh_token in either .access_token (cloud PAT path)
# or .api_key (device local admin path). jq's `//` DOES NOT fall back on empty
# string, so filter both explicitly.
REFRESH=$(jq -r '[.connectors.spotify.access_token, .connectors.spotify.api_key]
                  | map(select(. != null and . != ""))
                  | .[0] // ""' "$CFG")
CLIENT_ID=$(jq -r '.connectors.spotify.credentials.client_id' "$CFG")
CLIENT_SECRET=$(jq -r '.connectors.spotify.credentials.client_secret' "$CFG")

if [ -z "$REFRESH" ]; then
  echo "no refresh_token stored — ask user to reconnect Spotify in Settings"
  exit 1
fi

# Basic auth via stdin so client_secret never appears on the command line.
TOKEN_JSON=$(printf 'Authorization: Basic %s' \
    "$(printf '%s:%s' "$CLIENT_ID" "$CLIENT_SECRET" | base64 | tr -d '\n')" \
  | curl -sS -H @- -H 'Content-Type: application/x-www-form-urlencoded' \
      --data-urlencode "grant_type=refresh_token" \
      --data-urlencode "refresh_token=$REFRESH" \
      https://accounts.spotify.com/api/token)

ACCESS=$(echo "$TOKEN_JSON" | jq -r '.access_token // empty')
NEW_REFRESH=$(echo "$TOKEN_JSON" | jq -r '.refresh_token // empty')

if [ -z "$ACCESS" ]; then
  echo "mint failed: $TOKEN_JSON"   # invalid_grant / invalid_client — reconnect
  exit 1
fi

# Rotate: preserve which slot the writer used (see the // fallback above).
if [ -n "$NEW_REFRESH" ] && [ "$NEW_REFRESH" != "$REFRESH" ]; then
  SLOT=$(jq -r 'if (.connectors.spotify.access_token // "") != ""
                then "access_token" else "api_key" end' "$CFG")
  TMP=$(mktemp)
  jq --arg r "$NEW_REFRESH" --arg slot "$SLOT" \
    '.connectors.spotify[$slot] = $r' "$CFG" > "$TMP" && mv "$TMP" "$CFG"
fi

# Export so subsequent tool calls in the same turn can pass it via Bearer.
# ACCESS is the short-lived Bearer for api.spotify.com from here on. NEVER
# send REFRESH / CLIENT_SECRET to api.spotify.com.
echo "$ACCESS"
```

The command prints the fresh access_token to stdout so you can capture it in the next tool call. Save it in a shell variable (`ACCESS=...`) and pass it via `Authorization: Bearer $ACCESS` on every subsequent `api.spotify.com` request in this turn. Do NOT Bearer `$REFRESH`, `$CLIENT_SECRET`, `.api_key`, or `.access_token` from disk — only Bearer the `$ACCESS` you just minted.

### Step 2: pick a target device

`GET /me/player/devices` returns the connected Spotify Connect devices (phone / desktop / Intern 2 speaker if Soloist is running). Prefer a device whose name matches `intern` (case-insensitive contains); otherwise take the first entry. If `.devices` is empty → speak *"Bạn mở Spotify trên điện thoại hoặc máy tính giúp tao trước rồi mở lại nha, tao chưa thấy loa nào để phát."* and stop.

```bash
DEVICE_ID=$(printf 'Authorization: Bearer %s' "$ACCESS" | curl -sS -H @- \
    https://api.spotify.com/v1/me/player/devices \
  | jq -r '(.devices[] | select(.name | test("intern"; "i")) | .id),
           (.devices[0].id // empty)' | head -1)
```

### Step 3: resolve the request → a Spotify URI

- **Specific track / artist** → `GET /v1/search?type=track&limit=1&q=<query>` → `.tracks.items[0].uri` (a `spotify:track:...` URI).
- **Album name** → `GET /v1/search?type=album&limit=1&q=<query>` → `.albums.items[0].uri` (a `spotify:album:...` context URI).
- **Playlist name / genre / mood** → `GET /v1/search?type=playlist&limit=1&q=<query>` → `.playlists.items[0].uri`.
- **Any song / "just play something"** → search a broad chill playlist: `q=chill mix&type=playlist&limit=1`.
- **"Favorite album" / vague personal ask** → check habit patterns first (same file the YouTube path checks — see below). If none → ask the user *"Album nào của bạn tao mở đây?"* — a personalized ask, not a generic "what music?".

Search example:
```bash
URI=$(printf 'Authorization: Bearer %s' "$ACCESS" | curl -sS -G -H @- \
    --data-urlencode "q=<query>" --data-urlencode "type=track" --data-urlencode "limit=1" \
    https://api.spotify.com/v1/search \
  | jq -r '.tracks.items[0].uri // empty')
```

### Step 4: start playback

For a single track use `{"uris":["spotify:track:..."]}`; for an album / playlist / show use `{"context_uri":"spotify:album:..."}` (or `spotify:playlist:...`). Pass the picked device id in the query string:

```bash
# Track:
printf 'Authorization: Bearer %s' "$ACCESS" | curl -sS -X PUT -H @- \
  -H 'Content-Type: application/json' \
  --data "$(jq -nc --arg u "$URI" '{uris:[$u]}')" \
  "https://api.spotify.com/v1/me/player/play?device_id=$DEVICE_ID"

# Album / playlist (context_uri instead of uris array):
printf 'Authorization: Bearer %s' "$ACCESS" | curl -sS -X PUT -H @- \
  -H 'Content-Type: application/json' \
  --data "$(jq -nc --arg u "$URI" '{context_uri:$u}')" \
  "https://api.spotify.com/v1/me/player/play?device_id=$DEVICE_ID"
```

A successful play returns HTTP 204 with an empty body.

### Step 5: confirm with an emotion marker + short TTS line

Emotion marker is still required — see the Genre → Emotion table below. No `[HW:/audio/play:...]` marker on this path — Spotify Connect already started playing when the PUT succeeded.

```
[HW:/emotion:{"emotion":"happy","intensity":0.8}] Đang mở nhạc cho bạn.
```

### Step 6: stop

```bash
printf 'Authorization: Bearer %s' "$ACCESS" | curl -sS -X PUT -H @- \
  "https://api.spotify.com/v1/me/player/pause"
```
Reply: `[HW:/emotion:{"emotion":"calm","intensity":0.5}]` Music stopped.

### Spotify errors

- **`401 access_token expired`** → re-run the Step 1 mint block once (access tokens last ~1h and this may be a boundary case). Retry the failing call ONCE with the new access_token.
- **`401 Invalid access token`** on the mint response — the response JSON was `{"error":"invalid_grant"}` or `{"error":"invalid_client"}`. The refresh_token was revoked (user hit "REMOVE ACCESS" at spotify.com/account/apps or the 180-day window expired) or the Client Secret rotated. Speak *"Spotify của mày cần kết nối lại — vào Settings → Spotify trên thiết bị nha."* and stop.
- **`403 Player command failed: PREMIUM_REQUIRED`** → the account is Free OR the refresh_token was minted without `streaming` scope. Speak *"Spotify của mày không có Premium, tao không phát được — dùng YouTube thay hen?"* and immediately retry via the YouTube path.
- **`404 Device not found`** → the picked device id is stale (user's phone went offline). Refetch `/me/player/devices` right before the play call; do not cache device ids across turns. If the list is empty, use the "no devices" line from Step 2.
- **`429 Too Many Requests`** → back off — no retry. Speak *"Spotify đang giới hạn — thử lại sau chút nha."*.

## Workflow — YouTube path (fallback when Spotify not connected)

**Spoken output:** Everything outside HW markers in your reply is read aloud. For a play/stop request, start with the HW markers, then give one short confirmation and end the reply. Keep song-selection reasoning, transcript interpretation, and speaker attribution internal; do not add a preamble, a draft confirmation, or a second confirmation.

1. **Specific song / artist** → play directly. When `[voice-instruction]` is present, use that request; a conflicting noisy `[transcript]` does not replace the song title. No habit or identity lookup is needed. Use a known speaker for `person`; otherwise omit the field silently.
2. **Vague request** (*"play music"*, *"sing something"*) → check habit patterns first:
   ```bash
   cat /root/local/users/{name}/habit/patterns.json 2>/dev/null
   ```
   If `music_patterns` exists and current hour is within `peak_hour ± 1` → use `preferred_genre` to pick a song, no need to ask.
   Otherwise → ask: *"What are you in the mood for?"*. The file is bootstrapped lazily by wellbeing on its first threshold nudge; do not invoke habit Flow A from here.
3. Reply format:
   ```
   [HW:/audio/play:{"query":"Bohemian Rhapsody Queen","person":"alice"}][HW:/emotion:{"emotion":"excited","intensity":0.8}] Playing Bohemian Rhapsody!
   ```
4. Stop: `[HW:/audio/stop:{}] Music stopped.`

## API schema (`/audio/play`)

| Field | Required | Description |
|---|---|---|
| `query` | **YES** | YouTube search string (include artist for better match) |
| `person` | no | Who requested, lowercase (e.g. `"alice"`) — omit if unknown |

Do NOT use `track`, `artist`, `title`, `song` — those return 422.

## Genre → Emotion (pair with every `/audio/play`)

| Genre keywords | Emotion |
|---|---|
| jazz, blues, soul, funk, swing | `happy` |
| classical, orchestra, piano, violin | `curious` |
| hip hop, rap, trap, r&b, rock, metal | `excited` |
| anything else | `happy` |

## Examples

| Input | Output |
|---|---|
| *"Play Bohemian Rhapsody"* | `[HW:/audio/play:{"query":"Bohemian Rhapsody Queen","person":"alice"}][HW:/emotion:{"emotion":"excited","intensity":0.8}]` Playing Bohemian Rhapsody! |
| *"Sing me a song"* | `[HW:/emotion:{"emotion":"curious","intensity":0.6}]` What kind of vibe — chill, upbeat, or something specific? |
| *"Something chill"* | `[HW:/audio/play:{"query":"chill acoustic playlist","person":"alice"}][HW:/emotion:{"emotion":"happy","intensity":0.8}]` Here's some chill vibes! |
| *"Stop the music"* | `[HW:/audio/stop:{}]` Music stopped. |

Delegated request with an unknown speaker and noisy transcript:

Input:
```text
[voice-instruction] Play Eternal Flame
[transcript] Uh, my favorite song, uh, Ethan of Lamb.
```

Reply:
```text
[HW:/audio/play:{"query":"Eternal Flame The Bangles"}][HW:/emotion:{"emotion":"happy","intensity":0.8}] Playing Eternal Flame by The Bangles.
```

## How HW markers work

The Go server intercepts `[HW:/audio/play:...]` / `[HW:/audio/stop:...]` **in your reply text** and forwards to HAL. This is the ONLY way to play music — never use `exec`, `mpv`, `vlc`, `yt-dlp`, or `curl /audio/play`.

**The marker is passive text, NOT a command to run.** Write it directly in your reply and stop — the OS runs it for you. Do NOT try to "execute" or "invoke" it.

- ❌ **WRONG** — echoing/wrapping the marker in a shell tool. `echo` only prints to stdout inside your sandbox; the OS never sees it, so **no music plays**:
  ```bash
  echo '[HW:/audio/play:{"query":"Gymnopedie No 1 Satie"}]'
  ```
- ✅ **RIGHT** — the marker IS your reply text (no tool call at all):
  ```
  [HW:/audio/play:{"query":"Gymnopedie No 1 Satie"}][HW:/emotion:{"emotion":"curious","intensity":0.6}] Here's some Satie. 🎹
  ```

Never put `[HW:...]` inside `echo`, `exec`, `bash`, `printf`, or any tool argument. If you find yourself reaching for a tool to play music, stop — just emit the marker as text.

## Error handling

- `503` → *"Music playback is not available right now."*
- `409` → music already playing; stop first, then play new song.
- No results → tell user and suggest a different query.

## Rules

- **Emotion marker is mandatory** after every `/audio/play`.
- `person` MUST be lowercase.
- Don't recite lyrics or "sing" via TTS — call `/audio/play` and let real music play.
- Volume control belongs to the **Audio** skill, not this one.
- If user specifies genre or mood (*"play something relaxing"*), pick a well-known song — no need to ask further.
