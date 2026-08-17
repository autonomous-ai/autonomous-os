# What a turn costs

**Nothing here is measured yet.** This page is the method and the command; the
numbers land when someone runs it on a robot. Until then the only timings in
this tree are code comments — `~50 ms` for a local table match in
`system/intent/intent.go`, `3–5 s` through the agent in one comment and `8 s`
time-to-first-token in another. Two comments that disagree are not a spec, and
we will not publish them as one.

## Method

Every turn already writes a flow log: one JSONL line per stage, with a trace id
and a duration (`system/lib/flow/`, `GET /api/agent/flow-logs`). So there is
nothing to instrument — talk to the robot for a while, then read what it
recorded:

```bash
make latency TARGET=lamp-ac82.local PASSWORD=ac82
```

`PASSWORD` is the four characters in the robot's Wi-Fi name. Add `DATE=YYYY-MM-DD`
for an earlier day. To work from a downloaded log instead:

```bash
python3 scripts/bench/latency.py --file flow_2026-08-16.jsonl
```

It prints p50 / p95 / max per stage and for the whole turn, plus the two lines
that matter: how long a fixed command takes on the local path (`intent_match`,
no model, no network) and how long a turn through the brain takes
(`agent_call`).

These are real turns, not synthetic ones — the sample is however many turns that
robot actually had, and the report says so.

## What is still not covered

- **Time to first spoken word.** The flow log records when TTS was sent, not
  when sound left the speaker. Measuring the acoustic end needs a microphone
  and a clap track.
- **Power.** No watts, idle or moving. A USB meter and a fixed script would do
  it.
- **Memory and CPU.** `os-server` and HAL resident size, and CPU during a turn.
  One `top` capture on a Pi 5 Lamp would settle it.
- **Uptime.** The longest unattended run and how many OTA rollouts it survived.
  Nobody has tracked this.

## When you have numbers

Paste the table under a dated heading here, say which body and which brain, and
keep the raw JSONL next to it. Then the README can stop hedging — that is the
whole point of this file.
