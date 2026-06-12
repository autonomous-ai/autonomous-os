# Flow C — Familiar-stranger prompt (HAL surfaces the hint)

HAL tracks how often each `stranger_id` is seen. When the visit count first reaches `_FAMILIAR_VISIT_THRESHOLD` (=2), HAL:

1. Saves the current raw frame to `<STRANGERS_DIR>/snapshots/<stranger_id>_<ts_ms>.jpg`.
2. Appends a hint to the outgoing `presence.enter` message:

```
(familiar stranger <stranger_id> — seen <N> times, ask user if they want to remember this face; image saved at <path>)
```

Still self-enrollment in spirit — the prompt is addressed **to the person standing in front of the camera**, inviting them to introduce themselves. They're the subject and the responder; the owner on Telegram (if any) is just overhearing.

## Trigger
- The current sensing message contains the hint pattern above.
- The next reply gives a name — first-person ("I'm Alice"), relayed ("her name is Alice"), or just the bare "Alice".
- The reply declines ("no", "skip", "ignore") — acknowledge and stop.

## Steps

1. Parse `<stranger_id>` and `<path>` from the hint.
2. **Address the camera-person directly** in a single natural message — do NOT enroll yet:
   "I've seen you {N} times now — mind if I remember you? What's your name?"
3. Wait for the next reply.
4. **If a name is given** (with or without "yes"):
   - Lowercase the name → `label`.
   - Base64-encode the file at `<path>`.
   - Call `POST /face/enroll` with `image_base64`, `label`. **Omit `telegram_username` and `telegram_id`** — the camera-person isn't the Telegram sender, so any Telegram metadata in context belongs to someone else.
   - Confirm: "Got it, I'll remember you as {Name} from now on."
5. **If the reply declines** ("no" / "skip" / "ignore"): acknowledge once ("Okay, I won't ask again.") and stop. HAL will not re-prompt for the same `stranger_id` (the threshold fires only once per id).
6. **If the reply is ambiguous** ("maybe later", silence-ish): treat as decline.

## One-shot rule
The HAL hint surfaces exactly once per stranger when the count first reaches the threshold. Don't re-ask in later turns even if you see the same `stranger_id` again — only act on the hint when it appears in the current sensing message. Visit counts above the threshold do not re-fire.

## Example

```
[sensing] Person detected — 1 face(s) visible (stranger (stranger_37)) (familiar stranger stranger_37 — seen 2 times, ask user if they want to remember this face; image saved at /root/local/strangers/snapshots/stranger_37_1735...jpg)

Agent (turn 1):
  Reply: "I've seen you 2 times now — mind if I remember you? What's your name?"

User (turn 2): "I'm Alice."

Agent (turn 2):
  → POST /face/enroll {"image_base64": "<base64 of /root/.../stranger_37_1735...jpg>", "label": "alice"}
  → confirm: "Got it, I'll remember you as Alice from now on."
```
