# Gemini session reliability

Scope: `crates/gemini` and `crates/live/src/provider_worker.rs`.

The shared branch integrates Agent 1 through `d6b813695` with additional review
corrections. See [integration qualification](provider-recovery-integration.md)
for the combined source, repeat-disconnect fix, startup behavior and new gates.
The verification tables below retain Agent 1's earlier source and evidence;
they are not results for the later shared tree.

Evidence in this document comes from four separate places. They are never
combined:

| Class | What ran | What it can show |
|---|---|---|
| Host | macOS x86_64, scripted in-memory service | Transport and worker logic, bounds, ordering |
| Linux/ARM64 | Cross-compiled in a container, tests under user-mode emulation | The target builds, type-checks and runs the same logic |
| Cloud | **Nothing.** No provider credential exists on this host | Nothing yet; see [Service probes](#service-probes) |
| Device | **Nothing.** Lamp access is not restored | Nothing |

Passing these tests does not establish natural voice interaction. It
establishes that the listed failures no longer occur under the listed service
behavior. What the live service actually does in the ambiguous cases is
[unverified](#unqualified-behavior), and behavior that rests on an assumption
is reported as such at run time.

## Failures reproduced before fixing

"Ends the session" meant the provider worker exited and the whole run failed.

First round, reproduced on commit `64529dee4` with a scripted service:

| Scenario | Before | Now |
|---|---|---|
| Delayed `interrupted` arrives while the person speaks the next question | The new question was cancelled, then the session ended with `BarrierTimeout` | Dropped as `LateInterruption`; the question is unaffected |
| Duplicate completion arrives while the next question is being spoken | Session ended with `UnexpectedResponse` | Dropped as `LateTerminal` |
| Stray `interrupted` + completion arrive after the next question ended | The new question was completed with no answer | Both dropped; the real answer is delivered |
| Output transcript trails its answer's completion | Session ended with `UnexpectedResponse` | Attributed to the request that spoke it |
| 400 chunks arrive at once after a network stall | Session ended with `Backpressure`; 0 audio events delivered | All 400 delivered in order |
| Interruption whose confirmation takes longer than 0.8 s | Session ended after 803 ms and 70 blocks | Audio is never held |
| Answer still arriving after the response deadline | Cut with `ResponseTimeout` after 2 of 9 chunks | Deadline applies to the first output only |
| Service closes right after a complete answer | The answer's audio was dropped | Audio, generation and completion are delivered, then the end |
| Second interruption before the first is confirmed | Session ended with `OverlappingInput` | The waiting request is replaced |
| `goAway` during an answer | Session ended at once | The answer completes, then the connection is replaced |
| Any connection loss, including the idle close | Worker exited; run failed | Recovered when nothing accepted can be lost or repeated |

Second round, on commit `fbf9ecc3c`. The first two rows were reproduced in
real time with a consumer draining exactly 24,000 samples per second and the
largest (2 s) messages; the third is that commit's own test of its default.

| Scenario | Before | Now |
|---|---|---|
| 60 s answer, no pause | All 60 s delivered, but the session ended with `Backpressure`, a false delivery timeout | Completes; the connection stays ready |
| 48 s answer, playback paused 3 s after 6 s | Session ended with `Backpressure`; 38.0 of 48.0 s delivered | Completes; pauses up to the delivery bound are tolerated |
| Person resumes before any answer and the service stays silent | Session ended with `BarrierTimeout` after 2 s | The request proceeds; the assumption is reported |

Two further weaknesses of that commit were not reproduced; they follow from
its design and are covered by tests of the new one:

- Output was limited to 16 events plus one message, so an answer played at
  speaking speed stayed on the socket and a connection lost during playback
  would have lost the rest. Now the whole answer plays out and the link is
  replaced meanwhile.
- The worker's queue of up to 384 packets (15 s) was not purged on
  interruption, so once output is paced by playback the next answer would
  have waited behind it. Unsent output of a retired request is now pruned
  (the shared output-capacity change) and cancelled output is purged from the
  transport's buffer. The first packet of the new answer follows admission by
  5 ms (host, scripted service); at most the two packets already
  sent under outstanding capacity arrive late and are discarded.

V1 measured the service closing a session nobody talks to after 86-198 s with
WebSocket code 1008. That alone ended a V2 run after any long pause.

## Request ownership

Provider activity detection is disabled, so only this client's explicit
`activityStart` can interrupt a response, and the service cannot answer an
activity before its `activityEnd`. The transport uses exactly those two facts.

It keeps one **current request**, the newest, which is the only one that
accepts input, and at most one **earlier response** that must settle before the
current request may be committed with `activityEnd`.

- **Opening words are never held.** A new request sends `activityStart` and
  its audio at once, in order, whatever the earlier response is doing. Only its
  `activityEnd` waits. Until that End is sent, everything the service sends
  belongs to the earlier response by construction and is dropped.
- **Interruption is requested, never observed.** `interrupted` is honored only
  for a response this client has retired. Otherwise it is the delayed result of
  an earlier `activityStart`: dropped as `LateInterruption`. It cannot cancel a
  request and is not evidence that anyone spoke.
- **A stray pair stays a pair.** After a dropped `interrupted`, a completion
  that follows within the barrier bound and before any output of the current
  request is dropped as its companion.
- **Stray output re-arms the barrier.** Output with no request on record, or
  after a response's idle completion, is dropped and holds the next commit
  until it settles, exactly like a response this client retired.
- **Superseded input is not committed.** A request replaced before its End was
  sent continues in the same activity; it never gets a response of its own. A
  request cancelled before its End is dropped without `activityEnd`.
- **Output transcripts trail.** One that arrives before the current request
  has produced output is attributed to the previous responder. Input
  transcripts stay session-scoped and are never attached to a request.

### When the service does not confirm

The earlier response settles on its idle completion. If the service sends
neither that nor any further output for `Timeouts::barrier` (2 s):

| `BarrierPolicy` | Result |
|---|---|
| `Require` (library and production worker default) | `BarrierTimeout`; the connection fails |
| `AssumeAfterQuiet` (explicit transport/test policy) | The response is treated as over; `Event::BarrierAssumed` names both requests; the next request is committed |

The bound restarts on every event of the earlier response, so a slow but
active response is waited for. If it produces output again after an assumption
and before the next request is committed, the barrier is re-armed. A response
that is still producing 30 s (`Timeouts::response`) after it was asked to stop
fails the connection under either policy, so the next request cannot wait
forever.

Integration deliberately retains `Require`. The live endpoint has not been
probed for cancellation ordering, and the coordinator does not yet consume
`ownership_assumed`. Silence cannot qualify a late answer's owner. The explicit
assumption policy remains covered in tests and in the service probe tool, but
is not selected by the production worker or exposed in its configuration.

## Unqualified behavior

The transport cannot resolve these. Each is reported, never hidden, and none
is evidence of correct ownership.

1. **Answer after an assumed barrier.** The service stays silent for the whole
   bound, the next request is committed, and only then does an answer arrive.
   Nothing on the wire says which request it answers. It is delivered under the
   committed request, after `BarrierAssumed` for exactly that request.
   Preventing it would require the service to confirm interruptions, which is
   [probe 1](#service-probes).
2. **Lone completion before any output.** A completion that arrives after the
   current request was committed and before its first output cannot be told
   from an empty answer. It completes the request with no audio, and a real
   answer arriving afterwards is dropped as unowned. The coordinator records
   `no_audio_answer`.
3. **What the model heard.** A superseded request's audio stays in the activity
   its successor continues, and a cut-off question stays in the conversation.
   Whether the model answers the whole thought or only the last part is model
   behavior, not transport behavior.
4. **Conversation after a reconnect.** Context is whatever the service restores
   from its handle. This transport reports which kind of handle it used; it
   cannot inspect what the service remembers.

## Bounds

| Bound | Value | On expiry or overflow |
|---|---|---|
| Output held for a consumer slower than the network | 300 s of audio (14.4 MB), configurable 1-600 s | Socket goes unread; transport flow control holds the service |
| Event queue to the worker | 16 events | Events wait in the output buffer |
| Consumer accepts nothing while output waits (`deliver`) | 30 s (was 2 s) | `Backpressure` |
| Worker IPC backlog | high-water 384 packets, hard bound 512 | Worker stops taking provider events |
| Worker packets per 2 ms tick | 8, and never beyond granted output capacity | Remainder waits |
| Silence of an earlier response after an interruption request (`barrier`) | 2 s | Policy above |
| Earlier response still producing after an interruption request | 30 s (`response`) | `BarrierTimeout` |
| Ended input to first output (`response`) | 30 s | `ResponseTimeout` |
| Silence between outputs before generation completes (`stall`) | 10 s | `StalledResponse` |
| Idle completion after generation (`completion`) | received audio duration + 15 s | `CompletionTimeout` |
| Ended input to idle completion (`turn`) | 300 s | `ResponseTimeout` |
| Reconnect attempts per outage | 4; waits of 0.5, 1 and 2 s | `AttemptsExhausted` |
| Connection loss to completed setup | 15 s | `BudgetExhausted` |
| Recoveries per 60 s | 3 | `Unstable` |

`deliver` must exceed the playing time of the longest message, 2 s, plus the
longest pause in playback that should be survived. It measures a consumer that
takes nothing at all; one that is slow but still taking events never trips it.

By design, and not exercised by a test: an answer longer than about 285 s
reaches the `turn` bound before the service's idle completion. Everything
received is still delivered, and the request is then settled or reported lost
according to whether generation had completed.

## Speaking speed

The service generates faster than a speaker plays. The transport therefore
receives an answer at network speed and holds it, so playback speed does not
decide how long the connection must survive, and a connection that dies during
playback does not take the rest of the answer with it. Only beyond the output
bound does the socket go unread.

Commands, local retirement and shutdown stay live throughout. One server
message is handled per scheduling turn. Cancelled output is purged from the
transport's buffer and from the worker's queue the moment its request is
retired, so the next answer is not delayed by audio nobody will hear.

Timing relies on the service answering WebSocket pings during the long quiet
period between generation and its idle completion.

### Output capacity

The coordinator and the worker share the cumulative packet limit defined in
[provider output capacity](provider-flow-control.md), which belongs to the
coordinator's owner: `Control::ProviderOutputCapacity { through }`, granted
from the free space of the coordinator's 30 s reply buffer, with at most two
packets outstanding. The provisional `AudioCredit` seam this worker carried
before that contract existed is gone.

What the worker does with it:

- Every packet consumes capacity: audio, lifecycle events, transcripts,
  `Ready` and the proposed statuses alike. Nothing is sent without it.
- Link status (`unavailable`, `recovered`) is queued ahead of audio but still
  waits for capacity. At playback speed that is at most one packet, 40 ms. With
  a full reply buffer and stopped playback it waits as long as playback does.
- Unsent output of a retired request is pruned on every tick, before anything
  is sent. Failure and link statuses are never pruned.

A long answer therefore sits in four places, each bounded, from the speaker
back to the service:

| Holder | Bound |
|---|---|
| Coordinator reply buffer | 30 s (720,000 samples) |
| Worker queue | 512 packets (20.5 s); stops taking events above 384 (15.4 s) |
| Event queue to the worker | 16 events |
| Transport output buffer | 300 s; beyond it the socket goes unread |

At speaking speed the worker's queue falls below its high-water mark, and so
takes one more event from the transport, about every 2 s: the playing time of
the largest message. The `deliver` bound of 30 s therefore tolerates playback
stopping for up to about 28 s with every holder full. Longer than that is
treated as a consumer that has stopped: the connection ends with
`Backpressure`, which is not recovered, and audio the transport still held is
dropped. Holds of 4, 10 and 20 s are tested through the worker in real time.
The 30 s bound itself is tested in the transport on a paused clock; the
derived 28 s figure is not.

## Connection loss

`lamp_gemini::Supervisor` owns one live connection at a time.

1. A failure is noticed at once, by the worker's 2 ms tick through
   `Supervisor::maintain`, not when the next event is asked for. Input is
   refused from that moment.
2. The replacement is dialed immediately on the runtime, with the retained
   resumption handle, while output the failed connection had already received
   keeps being delivered in order at playback speed.
3. When that output is exhausted, the request that was in flight is reported:

| Request state when the output ran out | Result |
|---|---|
| None, or cancelled locally | Nothing to report |
| Generation complete | `Settled`: the closed connection is the idle barrier; the worker emits `TurnComplete { idle: true }` |
| Input still open | `TurnLost { InputOpen }` |
| Input ended, no output | `TurnLost { AwaitingResponse }` |
| Output began, generation incomplete | `TurnLost { Responding }` |

**Nothing is replayed.** Input is never re-sent and no answer is requested
twice. A lost request's identifier is retired across the reconnect. A
follow-up during the drain goes to the replacement connection and cancels what
remained of the old answer. A local shutdown or privacy stop drops queued
output.

Quota, rate-limit and usage-limit refusals, and authentication, configuration
and protocol faults, do not reconnect. Server text is never retained: a close
reason or error body only selects a fixed category.

Context survives only through the service's resumption handle
(`Resumption::Retain`: initial setup unchanged, handle kept in memory, never
logged or persisted). With `require_context` on and no handle, the worker does
not continue with a session that has forgotten the conversation.

### Reporting to the coordinator

`Reporting::Legacy` is what runs today, because the coordinator implements only
this:

- A recovery is a repeated `provider_ready`; its `setup_us` spans loss to setup.
- `TurnLost`, and input admitted during an outage, end the worker with the
  request and stage in the error.
- One standard-error line per outage, recovery, settled answer, assumed
  barrier, announced close and first dropped event of each kind. A clean run
  writes nothing.

`Reporting::Typed` is implemented and tested but not enabled. It keeps the
worker running and sends the statuses below. See the next section.

## Proposed coordinator contract

Not enabled. `ProviderOutput` is matched exhaustively in `coordinator.rs`, so
each addition needs a change by its owner. The statuses are `ProviderStatus` in
`provider_worker.rs`; they use the same envelope and `kind` tag and are meant
to become `ProviderOutput` variants unchanged.

```json
{"kind":"unavailable","reason":{"category":"peer_closed","code":1008}}
{"kind":"recovered","outage_us":1500000,"attempts":2,"context":"resumed_before_latest","reason":{"category":"server_go_away"}}
{"kind":"turn_failed","request":7,"stage":"awaiting_response","reason":{"category":"stalled_response"}}
{"kind":"ownership_assumed","request":8,"superseded":7}
```

| Status | Meaning | Coordinator obligation |
|---|---|---|
| `unavailable` | No connection. Sent ahead of queued audio | Stop admitting, or expect `turn_failed` for what is admitted |
| `recovered` | A connection is ready. `context` is `resumed`, `resumed_before_latest` or `fresh` | Resume admission; record the context |
| `turn_failed` | No further output for this request. `stage` is `not_delivered`, `input_open`, `awaiting_response` or `responding`. Sent after all output that did arrive | Give the turn an honest failed outcome and cancel it; this is the hook for a spoken failure |
| `ownership_assumed` | See [Unqualified behavior](#unqualified-behavior) | Mark the answer's outcome as unqualified |

`reason.category` is one of `connect_timeout`, `setup_timeout`, `read_timeout`,
`write_timeout`, `barrier_timeout`, `response_timeout`, `stalled_response`,
`completion_timeout`, `authentication`, `rate_limited`, `quota_exceeded`,
`server_unavailable`, `server_rejected`, `server_go_away`, `transport`,
`peer_closed` (with `code`), `protocol`, `local`.

Each `turn_failed` is sent once. Remaining input for a failed request is
dropped by the worker until the coordinator retires it, and never reaches a
later session.

Also needed from the coordinator's side:

- When the statuses are adopted they are packets like any other: the
  coordinator must count each one with `OutputReceiver::received` or the
  capacity counters diverge.
- A decision on whether `unavailable` should be able to pass a full reply
  buffer. Today it cannot, by the rule that every packet reserves space.
- A `ProviderConfig` option for `Resumption::Request` and the barrier policy.
- The startup poll now accepts a repeated `Ready` while later children start,
  preserving the original output-capacity counters. Other unsolicited output
  before input is still rejected. This does not implement outage admission.
- `provider_interrupted` can no longer arrive for the current reply.

## Service probes

No cloud probe has been run: the provider configuration and credential exist
only on the device. `crates/gemini/examples/service_probe.rs` is ready for
whoever has access. It needs no microphone, speaker or device. It speaks one
cached question and prints one JSON line of event kinds and timings: no
transcript, audio, handle or credential.

```sh
cargo run --locked -p lamp-gemini --example service_probe -- \
    /absolute/private/provider.json QUESTION.wav PROBE
```

| Probe | Question it answers | Decides |
|---|---|---|
| `interrupt-before-output` | After `activityEnd` and before any output, does `activityStart` produce `interrupted`, a completion, or nothing? | Whether pause-and-resume ever takes the assumed path |
| `handles` | Are resumption handles issued without being requested? | Whether recovery is available on this endpoint as configured |
| `handles-requested` | Is `sessionResumption: {}` accepted in setup? | Whether to switch to `Resumption::Request` |
| `resume` | Is a handle accepted on a new connection, and does that session answer? | Whether context survives a reconnect at all |
| `idle-close` | How long does an unused session live and how does it end? | The idle-close code and timing assumed from V1 |
| `fixture` | Offline: what would be spoken from the WAV file | Nothing about the service |

`fixture` was run on the cached greeting: a 20 s file, 2.31 s spoken, 231
blocks. The other five have never run.

## Verification

From `lampOS/`, with a fresh target directory:

```sh
cargo fmt --all -- --check
cargo clippy --locked --offline --workspace --all-targets -- -D warnings
cargo test --locked --offline --workspace --no-fail-fast -- --test-threads=1
cargo build --locked --offline --release -p lamp-live -p lamp-voice-eval
cargo test --locked --offline -p lamp-gemini --lib soak -- --ignored --nocapture
cargo test --locked --offline -p lamp-live --lib follows_output_capacity -- --ignored --nocapture --test-threads=1
```

The last two are the prolonged conversations (about 150 s of CPU) and the
real-time 60 s and 180 s answers (270 s). `lamp_gemini::testing` is the
scripted service: in-memory, no TLS, socket, credential or cloud request.
Transport scenarios run on a paused runtime clock, so their timings are exact
rather than load-dependent. Worker scenarios run in real time, because the IPC
and authority clocks cannot be paused.

In the speaking-speed tests the harness stands in for the coordinator. It
counts every packet with the shared `OutputReceiver`, grants capacity with the
shared formula from the free space of a reply buffer, and drains that buffer
at 24,000 samples per second of measured time. Time spent on hold or with
nothing to play is never made up. The scripted service writes 2 s messages at
ten times speaking speed, stalls for 3 s a third of the way in, then sends
10 s with no gap, and withholds its idle completion for the answer's own
length. The test fails if a block is lost, repeated or reordered, if a packet
exceeds granted capacity, if the buffer overruns or never fills, if playback
runs dry for 100 ms, or if playback ends at any time other than the answer's
length plus the hold. **The real coordinator loop and speaker are not in these
tests.**

### Host results

macOS x86_64, Rust 1.96.0, source-only export of merge commit `a09b305bc`
outside the repository, fresh target directory:

| Gate | Result |
|---|---|
| Formatting | pass |
| Strict Clippy, workspace, all targets | pass |
| Workspace tests, serial | 755 passed, 0 failed, 3 ignored |
| Release build of `lamp-live` and `lamp-voice-eval` | pass |
| Prolonged conversations (ignored by default) | pass: 4 seeds x 400 turns, tallies unchanged |
| 60 s and 180 s real-time answers (ignored by default) | pass, run in the working tree on the same source |

`lamp-gemini` contributes 99 of those tests (81 unit, 6 setup, 12 codec) and
the provider worker 36. The count is higher than the 636 recorded for
`7a556b3ff` because the merge brought in the voice evaluator's suites.
`crates/gemini/**` is byte-identical to `7a556b3ff`.

### Linux/ARM64 results

Cross-compiled for `aarch64-unknown-linux-gnu` in a Debian bookworm container
on the x86_64 host (Rust 1.96.0, `aarch64-linux-gnu-gcc` 12.2.0), from a
read-only source-only export. Test binaries are real ARM64 Linux executables
run under `qemu-aarch64` 7.2 user-mode emulation. **This is not the device**: it
checks target compilation and the exercised code paths under emulation. It
does not establish device timing, ALSA hardware behavior or room audio.

All on merge commit `a09b305bc`:

| Gate | Result |
|---|---|
| Strict Clippy, workspace, all targets, ARM64 target | pass |
| Release build of `lamp-live` and `lamp-voice-eval` | pass: `lamp-live` is ELF 64-bit ARM aarch64, dynamically linked |
| `lamp-gemini` tests, debug and release builds | 99 passed, 0 failed, each |
| Provider worker tests, release build | 36 passed, 0 failed |
| 60 s real-time answer, release build | pass: playback ended at 70.02 s after a 10.00 s hold; backlog peaked at 30.00 s |
| Prolonged conversations, 4 seeds x 400 turns, release build | pass in 46 s; every tally identical to the host run |
| Provider worker tests, debug build | 33 of 36 passed |
| Workspace tests, release build, serial | 793 passed, 1 failed, 3 ignored |

The workspace count is higher than on the host because Linux-only suites run
there. Its one failure is outside this work:
`a_transport_that_closes_mid_trace_invalidates_the_attempt` in the voice
evaluator's `physical_loopback` suite expected an abort naming a partial
trace and got `session start failed: Broken pipe`. It passes on the host. It
has not been investigated here and belongs to that crate's owner.

The three debug-build failures were observed when the test harness missed
real-time deadlines. The harness plays the
coordinator: it must renew a 100 ms input lease and a 250 ms heartbeat while
the same single-threaded test process decodes megabytes of scripted audio in
unoptimized emulated code. When the heartbeat lapses the worker exits
(observed: broken pipe, connection refused); when output is not read in time
the harness gives up (observed: no provider output within 5 s). They are the
three tests that carry the most audio: the 80 s burst, the 20 s answer and
the interruption of a 40 s answer. Earlier commits failed two or three of the
same kind, varying between runs. The same binaries built with optimization
pass all 36 under the same emulation, and the debug build passes on the host.
Whether a debug build keeps these deadlines on the device itself is not known.

Measured under emulation, release build, same boundaries as the host table
below: interruption to first packet of the new answer 9 ms with 1 stale
packet; 80 s burst in 4.0 s; 20 s answer with a 4 s hold played out in
24.01 s.

### Measured on the host

Inside the test process, scripted service, real time unless marked virtual.
None of these is a network, answer-latency or audible measurement.

| Measurement | Boundary | Result |
|---|---|---|
| 20 s answer, 5 s reply buffer, 4 s playback hold | First tick to the last sample leaving a modeled speaker | 24.03 s; completion received at 20.00 s; service wrote it in 4.3 s; 500 packets; backlog peaked at 5.00 s; 3 ms run dry |
| 60 s answer, 30 s reply buffer, 10 s hold | Same | 70.03 s; completion at 60.01 s; written in 8.5 s; 1,500 packets; peak 30.00 s; 1 ms run dry |
| 180 s answer, 30 s reply buffer, 20 s hold | Same | 200.05 s; completion at 180.01 s; written in 21.2 s; 4,500 packets; peak 30.00 s; 1 ms run dry |
| Interruption of a 40 s answer under capacity | Admission to first packet of the new answer | 5 ms in each of 2 runs; 1 stale packet each, of at most 2 possible |
| 80 s of audio, capacity returned on receipt | Service write to local IPC reader | 4.0-4.3 s for 2,000 packets over 3 runs |
| Stop during a hung reconnect | Control datagram to worker task end | 2.6-3.8 ms over 3 runs; 2.4-24.2 ms over 10 runs before the merge |
| 60 turns with injected failures (virtual) | Seed 1 | 5.3 min; 27 completed, 14 cancelled, 5 settled, 13 lost, 27 recoveries |
| 4 x 400 turns with injected failures (virtual) | Seeds 2, 3, 5, 8 | 52-70 min each; 843 completed, 417 cancelled, 107 settled, 232 lost, 477 recoveries, 145 assumed barriers in total |

The 80 s burst took 1.5 s before the merge. The difference is the capacity
contract doing its job: with two packets outstanding and a reader on a 2 ms
cadence, delivery to the coordinator is limited to about twenty times speaking
speed in this harness.

In every scripted conversation each request ended in exactly one outcome, no
audio was delivered under another request's name, and the service received
each request's input exactly once across all sessions.

## Integration notes

Branch `gemini-voice`. It contains shared checkpoint `4b8436d23` of
`lamp-v2-chat` by merge:

| Commit | Content |
|---|---|
| `fbf9ecc3c` | First round: ownership rules, delivery, bounded recovery |
| `9f22e541d` | Output buffer for speaking speed, drain after failure, barrier model and policy |
| `55193bb3c` | Supervisor recovers while output drains; worker purge, typed statuses |
| `a7b575918` | Service probe tool |
| `0489ef5d7` | Scripted audio built outside the real-time window in two worker tests |
| `7a556b3ff` | Bound on an interrupted response that keeps producing |
| `93a30b3cd` | This document, first version |
| the merge after it | `lamp-v2-chat` `4b8436d23`; the worker moves from its provisional seam to the shared output-capacity contract |
| the commit after that | This document, for the merged tree |

Changed files: `crates/gemini/**`, `crates/live/src/provider_worker.rs`, this
document, and the barrier sentence in `docs/live-runtime.md`. No shared
manifest, lock file, coordinator, admission, audio or choreography file is
touched, and `ProviderInput` / `ProviderOutput` are unchanged on the wire.

The merge conflicted only in `provider_worker.rs`. Everything the shared
change added there is kept as it was: the capacity sender, the packet-size
constant, the pruning rule and its tests. Two things differ from the shared
version, both because this worker's queue also carries statuses: the pruning
rule runs over that queue and never removes a status, and its unit test
checks that. The worker's own speaking-speed tests now use the shared
`OutputReceiver` exactly as the coordinator does.

The initial `fbf9ecc3c` transport had a two-second no-consumption watchdog.
The bound is now 30 s and the 60 s and 180 s delivery tests pass on the host.
The shared [flow-control notes](provider-flow-control.md) describe the updated
bound. [Root integration qualification](provider-recovery-integration.md)
records the combined source and distinguishes modeled playback from real
coordinator, speaker and room behavior.

What an integrator will notice:

- `lamp_gemini::Event` gained `BarrierAssumed`; `Discard::UnansweredBarrier`
  is gone. `Notice` gained `Unavailable`. Only the provider worker matches them.
- `Timeouts::deliver` is 30 s. `SessionConfig` gained `barrier_policy` and
  `output_buffer_seconds`.
- Agent 1's branch selected `BarrierPolicy::AssumeAfterQuiet`. Shared integration
  retains `Require` pending the service probes and explicit ownership handling.
- Provider memory can reach the output bound (14.4 MB) during a long answer.
- A recovery writes to the worker's inherited standard error.
- Do not build two checkouts into one cargo target directory: workspace crates
  share build identity by relative path and a stale test binary can run
  without warning.

## Required before relying on this

- The five service probes, on an endpoint with real credentials.
- A native build and test run on the device. Emulated execution covers the
  tested Linux/ARM64 paths; it does not establish general architecture
  independence, device timing or hardware behavior.
- A physical cohort that includes: an idle pause longer than 200 s followed by
  a follow-up that depends on earlier context; a question continued after the
  endpointer cut it; two interruptions in a row; a topic change before the
  answer starts; an answer of at least 60 s; a network link dropped while idle
  and mid-answer. Record every `provider_ready`, the worker's standard error
  and the room audio.
- Echo-driven false admissions are outside this work and unchanged by it. Each
  one still cancels the answer in progress.
