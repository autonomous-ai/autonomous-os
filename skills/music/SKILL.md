---
name: music
description: Play and stop music from YouTube through the lamp speaker on user request.
---

# Music

Play music through the lamp speaker by searching YouTube. Use this when the user asks to play, sing, or listen to music.

## Workflow

1. **Specific song / artist** → play directly.
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

## How HW markers work

The Go server intercepts `[HW:/audio/play:...]` / `[HW:/audio/stop:...]` and forwards to HAL. This is the ONLY way to play music — never use `exec`, `mpv`, `vlc`, `yt-dlp`, or `curl /audio/play`.

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
