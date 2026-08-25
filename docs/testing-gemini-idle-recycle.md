# Test procedure — Gemini idle-session recycle

**Tests:** branch `fix/hal-drop-unfed-gemini-session`
**Fixes:** a post-silence turn landing on a session Gemini already closed
(`WS 1008 "The operation was aborted"`), which the user experiences as *"I had to
say it twice"*.

## What is being tested

Gemini closes a Live session it receives nothing on. HAL holds sessions open while
feeding them nothing — room noise opens captures that fail STT and are dropped
before reaching the model, so from Gemini's side the session is silent even while
HAL is busy. Measured 2026-08-21 on `gemini-3.1-flash-live-preview`: closes after
**86–185 s** of nothing-sent, and **107 of 113** such closes landed on sessions
that had served zero turns.

`prepare_turn()` already guards against this by rebuilding an idle session before
the next turn streams — but it was gated on `gemini_needs_idle_workaround()`,
which is true only for `native-audio` models, so every 3.1 device ran unprotected.
The fix ungates it and lowers the threshold from 120 s to 60 s (120 s sat *inside*
the observed failure range, so it could fire after the session was already dead).

**The variable under test is the silence gap, not what you say.** Keep the
sentences short and watch the clock.

## Setup

```bash
# device under test
ssh orangepi@<device-ip>

# watch the decisive lines
journalctl -u hal -f -n0 --no-pager \
| grep -E "recycling Gemini before streaming audio|1008|Recv API error|No realtime output|Response latency"
```

Confirm the threshold is live before starting:

```bash
sudo /opt/hal/.venv/bin/python -c \
  "from hal import config as c; print(c.REALTIME_GEMINI_PRE_TURN_RECYCLE_S)"
# expect: 60.0
```

## Rounds

### 1 — Baseline, no gap
> "Hello, are you there?" → *(reply)* → "What's the weather like?"

Two turns back to back. Expect **no** recycle line — an active conversation must
not pay a handshake between turns.

### 2 — 90 second gap ← the main test
> "Tell me something interesting."
> **⏱ wait 90 s in silence**
> "Are you still with me?"

The case that used to break: past the 86 s floor where Gemini started closing
unfed sessions. Expect a recycle line before the second turn, and a normal answer.

### 3 — 3 minute gap
> "Count to five."
> **⏱ wait 3 min**
> "Okay, what did I just ask you to do?"

Past 185 s, the longest idle death measured. The turn must still answer. The reply
will **not** remember the counting — the session is fresh — and that is expected,
not a failure.

### 4 — 45 second gap (control)
> "Say hello."
> **⏱ wait 45 s**
> "Say hello again."

Below the 60 s threshold → expect **no** recycle. This is the round that catches
the fix being too eager; do not skip it.

### 5 — Repeat round 2, twice more
The original failure was intermittent. One clean pass proves nothing; three clean
90-second gaps is the minimum worth trusting.

## Reading the result

| observed | meaning |
|---|---|
| `Ns idle (>= 60s) — recycling Gemini before streaming audio` | the recycle engaged ✓ |
| answer arrives normally after the gap | the turn survived ✓ |
| `1008 The operation was aborted` | still dying — fix insufficient |
| `No realtime output (empty / timeout)` | the turn was lost — the symptom being fixed |
| recycle line in round 1 or 4 | threshold too low / too eager |
| no recycle line in rounds 2 or 3 | not wired — the fix is not reaching `prepare_turn` |

Rounds 1 and 4 are the guard rails. A run that only does rounds 2 and 3 cannot
distinguish "works" from "recycles constantly".

## Confounders to keep in mind

- **`HAL_VAD_THRESHOLD=500`** makes room noise open captures constantly. Those
  produce `Noise turn — nothing sent to model` lines and are what starve the
  session in the first place. Testing in a quiet room hides the very condition
  that causes the bug; testing in a noisy one is more faithful.
- **A recycle is not free** — it costs a ~1 s handshake and the new session starts
  from the reloaded `summary.md`, so short-term context is gone. Losing the thread
  across a long gap is expected behaviour, not a regression.
- **Session ages are only meaningful with turn counts.** A session that dies at
  150 s having served zero turns is an abandoned pre-connect (cosmetic); one that
  dies having served turns is a real conversation lost. Count both.

## Rolling back

The threshold is an env override — no redeploy needed:

```bash
# disable the recycle entirely
HAL_GEMINI_PRE_TURN_RECYCLE_S=0

# or restore the old default
HAL_GEMINI_PRE_TURN_RECYCLE_S=120
```

Full revert on a device patched by hand: restore from the `/opt/hal_bak_<ts>/`
directory created at deploy time, then `sudo systemctl restart hal`.

## Background

Proxy-side analysis, including why this is not a `campaign-api` ceiling:
`bff-campaign-service/docs/gemini-1008-idle-session-close.md`. Original report:
[autonomous-os#238](https://github.com/autonomous-ai/autonomous-os/issues/238).
