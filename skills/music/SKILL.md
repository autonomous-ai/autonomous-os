---
name: music
description: Play and stop music from YouTube through the device's speaker on user request.
---

# Music

Play music through the device's speaker by searching YouTube. Use this when the user asks to play, sing, or listen to music.

**Spoken output:** Everything outside HW markers in your reply is read aloud. For a play/stop request, start with the HW markers, then give one short confirmation and end the reply. Keep song-selection reasoning, transcript interpretation, and speaker attribution internal; do not add a preamble, a draft confirmation, or a second confirmation.

## Workflow

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
