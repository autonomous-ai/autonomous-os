# Gemini session reliability

Scope: `crates/gemini` and `crates/live/src/provider_worker.rs`.

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
  interruption, so under playback-paced credit the next answer would have
  waited behind it. Now the first packet of the new answer follows admission
  by 34-36 ms with 0 stale packets (host, scripted service).

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
| `Require` (library default) | `BarrierTimeout`; the connection fails |
| `AssumeAfterQuiet` (provider worker) | The response is treated as over; `Event::BarrierAssumed` names both requests; the next request is committed |

The bound restarts on every event of the earlier response, so a slow but
active response is waited for. If it produces output again after an assumption
and before the next request is committed, the barrier is re-armed. A response
that is still producing 30 s (`Timeouts::response`) after it was asked to stop
fails the connection under either policy, so the next request cannot wait
forever.

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
| Worker packets per 2 ms tick | 8, and never beyond granted audio credit | Remainder waits |
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

### Packet credit

The coordinator's cumulative packet credit is not defined here; it belongs to
the coordinator's owner. The worker has a provisional seam, `AudioCredit`: the
total number of audio packets the coordinator has had room for since the
worker started. The worker never sends audio beyond it. Lifecycle events wait
behind the audio they follow; link status does not. With no credit source the
worker behaves as before. What the provider side needs from the final design:

- Monotonic and cumulative, so a lost or repeated grant cannot double-count.
- Counts audio packets only. Purged packets were never sent and never count.
- Granted at playback pace, because the worker's own queue is 15 s deep.
- IPC freshness (100 ms) still applies to every packet once sent.

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

- Cumulative audio packet credit, as described above.
- `MAX_REPLY_SAMPLES` is a 30 s backlog. Without credit the worker still hands
  a long answer over as fast as the IPC allows, so an answer generated more than
  30 s ahead of playback reaches that bound.
- A `ProviderConfig` option for `Resumption::Request` and the barrier policy.
- A recovery during startup sends a second `Ready` that the startup poll
  rejects.
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
cargo build --locked --offline --release -p lamp-live
cargo test --locked --offline -p lamp-gemini --lib soak -- --ignored --nocapture
cargo test --locked --offline -p lamp-live --lib provider_worker -- --ignored --nocapture --test-threads=1
```

The last two are the prolonged conversations (about 150 s of CPU) and the
real-time 60 s and 180 s answers (270 s). `lamp_gemini::testing` is the
scripted service: in-memory, no TLS, socket, credential or cloud request.
Transport scenarios run on a paused runtime clock, so their timings are exact
rather than load-dependent. Worker scenarios run in real time, because the IPC
and authority clocks cannot be paused.

### Host results

macOS x86_64, Rust 1.96.0, source-only export of commit `7a556b3ff` outside
the repository, fresh target directory:

| Gate | Result |
|---|---|
| Formatting | pass |
| Strict Clippy, workspace, all targets | pass |
| Workspace tests, serial | 636 passed, 0 failed, 3 ignored |
| Release build of `lamp-live` | pass |
| Prolonged conversations (ignored by default) | pass: 4 seeds x 400 turns |
| 60 s and 180 s real-time answers (ignored by default) | pass, run on `55193bb3c`; the worker is unchanged since except for two test scripts |

`lamp-gemini` contributes 99 of those tests (81 unit, 6 setup, 12 codec) and
the provider worker 33. The same gates also passed on `a7b575918` (635 tests)
and `0489ef5d7` (635), and the first two code commits were each built, linted
and tested for both crates on their own export before the next was made.

### Linux/ARM64 results

Cross-compiled for `aarch64-unknown-linux-gnu` in a Debian bookworm container
on the x86_64 host (Rust 1.96.0, `aarch64-linux-gnu-gcc` 12.2.0), from a
read-only source-only export. Test binaries are real ARM64 Linux executables
run under `qemu-aarch64` 7.2 user-mode emulation. **This is not the device**: it
shows that the target builds, type-checks and behaves the same, and nothing
about device timing, ALSA hardware or audio.

| Gate | Commit | Result |
|---|---|---|
| Strict Clippy, workspace, all targets, ARM64 target | `7a556b3ff` | pass |
| Release build of `lamp-live` | `7a556b3ff` | pass: ELF 64-bit ARM aarch64, dynamically linked |
| `lamp-gemini` tests, debug and release builds | `7a556b3ff` | 99 passed, 0 failed, each |
| Provider worker tests, release build | `7a556b3ff` | 33 passed, 0 failed |
| Prolonged conversations, 4 seeds x 400 turns | `55193bb3c` | pass in 1,209 s; every tally identical to the host run |
| Provider worker tests, debug build | `55193bb3c`, `0489ef5d7` | 31 and 30 of 33 passed |
| Workspace tests, debug build, serial | `55193bb3c` | 672 passed, 2 failed, 3 ignored |

The workspace count is higher than on the host because Linux-only suites run
there. Its two failures are provider worker tests, and they are the debug-build
failures in the row above it.

Those failures are the test harness missing real-time deadlines, not a wrong
result from the worker. The harness plays the coordinator: it must renew a
100 ms input lease and a 250 ms heartbeat while the same single-threaded test
process decodes megabytes of scripted audio in unoptimized emulated code. When
the lease lapses the interaction controller revokes the turn, and the worker
correctly drops that turn's audio (observed: completion with no audio). When
the heartbeat lapses the worker exits (observed: broken pipe, connection
refused). Which tests miss varies between runs; the 20 s credit-paced answer
passed in one debug run and failed in the other. The same binaries built with
optimization pass all 33 under the same emulation, and the debug build passes
on the host. Whether a debug build keeps these deadlines on the device itself
is not known.

Measured under emulation, release build, same boundaries as the host table
below: interruption to first packet of the new answer 37 ms with 0 stale
packets; 80 s burst in 1.2 s; 20 s answer with a 4 s pause in 23.97 s.

### Measured on the host

Inside the test process, scripted service, real time unless marked virtual.
None of these is a network, answer-latency or audible measurement.

| Measurement | Boundary | Result |
|---|---|---|
| 20 s answer, 4 s playback pause | First credit to idle completion at a local IPC reader | 23.97 s; service wrote it in 2.1 s; 500 packets |
| 60 s answer, 10 s pause | Same | 69.96 s; written in 6.3 s; 1,500 packets |
| 180 s answer, 20 s pause | Same | 199.97 s; written in 18.9 s; 4,500 packets |
| Interruption under credit | Admission to first packet of the new answer | 34-36 ms; 0 stale packets |
| 80 s of audio with no credit limit | Service write to local IPC reader | 1.5 s for 2,000 packets |
| Stop during a hung reconnect | Control datagram to worker task end | 2.4-24.2 ms over 10 runs |
| 60 turns with injected failures (virtual) | Seed 1 | 5.3 min; 27 completed, 14 cancelled, 5 settled, 13 lost, 27 recoveries |
| 4 x 400 turns with injected failures (virtual) | Seeds 2, 3, 5, 8 | 52-70 min each; 843 completed, 417 cancelled, 107 settled, 232 lost, 477 recoveries, 145 assumed barriers in total |

In every scripted conversation each request ended in exactly one outcome, no
audio was delivered under another request's name, and the service received
each request's input exactly once across all sessions.

## Integration notes

Branch `gemini-voice`, on top of shared checkpoint `23bde4873`:

| Commit | Content |
|---|---|
| `fbf9ecc3c` | First round: ownership rules, delivery, bounded recovery |
| `9f22e541d` | Output buffer for speaking speed, drain after failure, barrier model and policy |
| `55193bb3c` | Supervisor recovers while output drains; worker purge, credit seam, typed statuses |
| `a7b575918` | Service probe tool |
| `0489ef5d7` | Scripted audio built outside the real-time window in two worker tests |
| `7a556b3ff` | Bound on an interrupted response that keeps producing |
| the commit after it | This document |

Changed files: `crates/gemini/**`, `crates/live/src/provider_worker.rs`, this
document, and the barrier sentence in `docs/live-runtime.md`. No shared
manifest, lock file, coordinator, admission, audio or choreography file is
touched, and `ProviderInput` / `ProviderOutput` are unchanged on the wire.

What an integrator will notice:

- `lamp_gemini::Event` gained `BarrierAssumed`; `Discard::UnansweredBarrier`
  is gone. `Notice` gained `Unavailable`. Only the provider worker matches them.
- `Timeouts::deliver` is 30 s. `SessionConfig` gained `barrier_policy` and
  `output_buffer_seconds`.
- The worker now uses `BarrierPolicy::AssumeAfterQuiet`. A person who resumes
  speaking no longer ends the run when the service is silent.
- Provider memory can reach the output bound (14.4 MB) during a long answer.
- A recovery writes to the worker's inherited standard error.
- Do not build two checkouts into one cargo target directory: workspace crates
  share build identity by relative path and a stale test binary can run
  without warning.

## Required before relying on this

- The five service probes, on an endpoint with real credentials.
- A native build and test run on the device. The emulated run shows the logic
  is architecture-independent; it says nothing about device timing.
- A physical cohort that includes: an idle pause longer than 200 s followed by
  a follow-up that depends on earlier context; a question continued after the
  endpointer cut it; two interruptions in a row; a topic change before the
  answer starts; an answer of at least 60 s; a network link dropped while idle
  and mid-answer. Record every `provider_ready`, the worker's standard error
  and the room audio.
- Echo-driven false admissions are outside this work and unchanged by it. Each
  one still cancels the answer in progress.
