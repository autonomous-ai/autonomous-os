# Checkin route — phrasing

Fires whenever the routing table in `SKILL.md` picks `checkin` (row #3 — the "anything else" catch-all: cooldown active so music is blocked, mood not suggestion-worthy, decision stale with no fresh synthesis, etc.). The music and LED-ack routes own their own output; this file owns checkin only.

## What "checkin" means

A short, human reaction to whatever the camera just caught. Three flavors to mix between — pick whichever fits the moment:

- **Ask** — a direct, curious question. ("What happened?")
- **Comfort** — gentle acknowledgment, no probing. ("I'm here.")
- **Invite** — open the door without demanding a reply. ("Want to talk?")

Keyed by **raw emotion** (the FER or voice label from the event), not by `mapped_mood`. Mood-bucket logic belongs to the music branch; checkin speaks to the immediate feeling.

Voice variants share the same row — `Fearful` → use `Fear`, `Surprised` → use `Surprise`, `Disgusted` → use `Disgust`. Same register, same phrasing inspiration.

## Example phrasing — INSPIRATION ONLY

> **DO NOT copy these strings verbatim.** They are flavor samples to show the register/style for each emotion. Each turn the agent should phrase its own line, drawing on these only as reference. Repeating the same wording across turns is the failure mode this section is meant to prevent.

English shown for clarity. Speak in the user's current language at runtime — translate or, better, rephrase from scratch in that language.

| Raw emotion | Ask (curious)                              | Comfort (acknowledge)                    | Invite (open door)              |
|-------------|--------------------------------------------|------------------------------------------|---------------------------------|
| `Sad`       | "What happened?" / "Did something hurt?" / "Why the tears?" | "I'm here, take your time." / "Whatever it is, I'm here." | "Want to talk?" / "Tell me what's wrong?" |
| `Fear`      | "What's worrying you?" / "What just happened?" / "Did something startle you?" | "It's okay to feel that." / "Breathe with me." | "Need a moment?" / "Want to slow down?" |
| `Angry`     | "What's eating you?" / "What hit a nerve?" | "That sounds rough." / "Take a beat."    | "Want to vent?" / "Need a break?" |
| `Disgust`   | "What's bothering you?" / "Did something annoy you?" | "Ugh, that's a lot."          | "Wanna step away?" / "Need a reset?" |
| `Happy`     | "What's the smile about?" / "What just happened?" | (skip — happy doesn't need comforting) | "Tell me!" / "Share?"           |
| `Surprise`  | "What just happened?" / "Something unexpected?" | (skip)                              | "Tell me, what is it?" / "Big news?" |

If the raw emotion is missing or unrecognized, fall back to a single open-ended line of the agent's own composition — improvise rather than freezing.

## Picking a style

These are nudges, not gates. Read context, pick whichever feels right; vary across turns:

- First time this emotion in the day, or `prior_decision` stale → **Ask** tends to fit (curiosity is warranted).
- Same emotion appeared 2+ times in the last 10 min via `recent_signals` → switch to **Comfort** or **Invite** (don't keep probing).
- Negative emotion (Sad / Fear / Angry / Disgust) on top of a stale stressed/sad decision → lean **Comfort** — the user is already in it, naming it again is heavier than acknowledging.
- Positive emotion (Happy / Surprise) → **Ask** or **Invite** only. No comfort branch.

The aim is variety. If the previous checkin this cooldown window was Ask, default to a different flavor this time.

## Reply format

Embed the log marker alongside `[HW:/emotion:...]` (and `[HW:/dm:...]` for known users). Replace `<emotion>` with the lowercased raw emotion label (`sad`, `fear` / `fearful`, `angry`, `disgust` / `disgusted`, `happy`, `surprise` / `surprised`) — keep the form HAL shipped so the trigger string stays trace-friendly.

- **Known user** (speak + DM):
  ```
  [HW:/emotion:{"emotion":"caring","intensity":0.5}][HW:/dm:{"telegram_id":"<id>"}][HW:/music-suggestion/log:{"user":"{name}","trigger":"checkin:<emotion>","message":"<one-liner>"}] <one-liner>
  ```
  `telegram_id` is in the injected `[user_info: ...]` block — never fetch.
- **Unknown user** (speak only):
  ```
  [HW:/emotion:{"emotion":"caring","intensity":0.5}][HW:/music-suggestion/log:{"user":"unknown","trigger":"checkin:<emotion>","message":"<one-liner>"}] <one-liner>
  ```

Log via the music-suggestion endpoint with `trigger:"checkin:<emotion>"` so `last_suggestion_age_min` covers all outreach channels (shared cooldown).

If the one-liner needs `}` (rare), fall back to:
```bash
curl -s -X POST http://127.0.0.1:5000/api/music-suggestion/log \
  -H 'Content-Type: application/json' \
  -d '{"user":"{name}","trigger":"checkin:<emotion>","message":"<one-liner>"}'
```

## Follow-up

One check-in per cooldown window. If the user doesn't reply, stay silent until the router routes again — don't chase.
